# Copyright 2016 - Nokia Networks.
# Copyright 2016 - Brocade Communications Systems, Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.

from oslo_config import cfg
from oslo_log import log as logging
from oslo_utils import timeutils
from osprofiler import profiler
import traceback as tb

from mistral.db.v2 import api as db_api
from mistral.engine import post_tx_queue
from mistral.engine import workflows
from mistral import exceptions as exc
from mistral.services import scheduler
from mistral.workflow import states

LOG = logging.getLogger(__name__)

CONF = cfg.CONF

_CHECK_AND_FIX_INTEGRITY_PATH = (
    'mistral.engine.workflow_handler._check_and_fix_integrity'
)


@profiler.trace('workflow-handler-start-workflow', hide_args=True)
def start_workflow(wf_identifier, wf_namespace, wf_ex_id, wf_input, desc,
                   params):
    wf = workflows.Workflow()

    wf_def = db_api.get_workflow_definition(wf_identifier, wf_namespace)

    if 'namespace' not in params:
        params['namespace'] = wf_def.namespace

    wf.start(
        wf_def=wf_def,
        wf_ex_id=wf_ex_id,
        input_dict=wf_input,
        desc=desc,
        params=params
    )

    _schedule_check_and_fix_integrity(wf.wf_ex, delay=10)

    return wf.wf_ex


def stop_workflow(wf_ex, state, msg=None):
    wf = workflows.Workflow(wf_ex=wf_ex)

    # In this case we should not try to handle possible errors. Instead,
    # we need to let them pop up since the typical way of failing objects
    # doesn't work here. Failing a workflow is the same as stopping it
    # with ERROR state.
    wf.stop(state, msg)

    # Cancels subworkflows.
    if state == states.CANCELLED:
        for task_ex in wf_ex.task_executions:
            sub_wf_exs = db_api.get_workflow_executions(
                task_execution_id=task_ex.id
            )

            for sub_wf_ex in sub_wf_exs:
                if not states.is_completed(sub_wf_ex.state):
                    stop_workflow(sub_wf_ex, state, msg=msg)


def force_fail_workflow(wf_ex, msg=None):
    stop_workflow(wf_ex, states.ERROR, msg)


def cancel_workflow(wf_ex, msg=None):
    stop_workflow(wf_ex, states.CANCELLED, msg)


@profiler.trace('workflow-handler-check-and-complete', hide_args=True)
def check_and_complete(wf_ex_id):
    wf_ex = db_api.load_workflow_execution(wf_ex_id)

    if not wf_ex or states.is_completed(wf_ex.state):
        return

    wf = workflows.Workflow(wf_ex=wf_ex)

    try:
        wf.check_and_complete()
    except exc.MistralException as e:
        msg = (
            "Failed to check and complete [wf_ex_id=%s, wf_name=%s]:"
            " %s\n%s" % (wf_ex_id, wf_ex.name, e, tb.format_exc())
        )

        LOG.error(msg)

        force_fail_workflow(wf.wf_ex, msg)


@post_tx_queue.run
@profiler.trace('workflow-handler-check-and-fix-integrity')
def _check_and_fix_integrity(wf_ex_id):
    check_after_seconds = CONF.engine.execution_integrity_check_delay

    if check_after_seconds < 0:
        # Never check integrity if it's a negative value.
        return

    # To break cyclic dependency.
    from mistral.engine import task_handler

    with db_api.transaction():
        wf_ex = db_api.get_workflow_execution(wf_ex_id)

        if states.is_completed(wf_ex.state):
            return

        _schedule_check_and_fix_integrity(wf_ex, delay=120)

        running_task_execs = db_api.get_task_executions(
            workflow_execution_id=wf_ex.id,
            state=states.RUNNING,
            limit=CONF.engine.execution_integrity_check_batch_size
        )

        for t_ex in running_task_execs:
            # The idea is that we take the latest known timestamp of the task
            # execution and consider it eligible for checking and fixing only
            # if some minimum period of time elapsed since the last update.
            timestamp = t_ex.updated_at or t_ex.created_at

            delta = timeutils.delta_seconds(timestamp, timeutils.utcnow())

            if delta < check_after_seconds:
                continue

            child_executions = t_ex.executions

            if not child_executions:
                continue

            all_finished = all(
                [states.is_completed(c_ex.state) for c_ex in child_executions]
            )

            if all_finished:
                # Find the timestamp of the most recently finished child.
                most_recent_child_timestamp = max(
                    [c_ex.updated_at or c_ex.created_at for c_ex in
                     child_executions]
                )
                interval = timeutils.delta_seconds(
                    most_recent_child_timestamp,
                    timeutils.utcnow()
                )

                if interval > check_after_seconds:
                    # We found a task execution in RUNNING state for which all
                    # child executions are finished. We need to call
                    # "schedule_on_action_complete" on the task handler for
                    # any of the child executions so that the task state is
                    # calculated and updated properly.
                    LOG.warning(
                        "Found a task execution that is likely stuck in"
                        " RUNNING state because all child executions are"
                        " finished, will try to recover [task_execution=%s]",
                        t_ex.id
                    )

                    task_handler.schedule_on_action_complete(
                        child_executions[-1]
                    )


def pause_workflow(wf_ex, msg=None):
    # Pause subworkflows first.
    for task_ex in wf_ex.task_executions:
        sub_wf_exs = db_api.get_workflow_executions(
            task_execution_id=task_ex.id
        )

        for sub_wf_ex in sub_wf_exs:
            if not states.is_completed(sub_wf_ex.state):
                pause_workflow(sub_wf_ex, msg=msg)

    # If all subworkflows paused successfully, pause the main workflow.
    # If any subworkflows failed to pause for temporary reason, this
    # allows pause to be executed again on the main workflow.
    wf = workflows.Workflow(wf_ex=wf_ex)
    wf.pause(msg=msg)


def rerun_workflow(wf_ex, task_ex, reset=True, env=None):
    if wf_ex.state == states.PAUSED:
        return wf_ex.get_clone()

    wf = workflows.Workflow(wf_ex=wf_ex)

    wf.rerun(task_ex, reset=reset, env=env)

    _schedule_check_and_fix_integrity(
        wf_ex,
        delay=CONF.engine.execution_integrity_check_delay
    )

    if wf_ex.task_execution_id:
        _schedule_check_and_fix_integrity(
            wf_ex.task_execution.workflow_execution,
            delay=CONF.engine.execution_integrity_check_delay
        )


def resume_workflow(wf_ex, env=None):
    if not states.is_paused_or_idle(wf_ex.state):
        return wf_ex.get_clone()

    # Resume subworkflows first.
    for task_ex in wf_ex.task_executions:
        sub_wf_exs = db_api.get_workflow_executions(
            task_execution_id=task_ex.id
        )

        for sub_wf_ex in sub_wf_exs:
            if not states.is_completed(sub_wf_ex.state):
                resume_workflow(sub_wf_ex)

    # Resume current workflow here so to trigger continue workflow only
    # after all other subworkflows are placed back in running state.
    wf = workflows.Workflow(wf_ex=wf_ex)
    wf.resume(env=env)


@profiler.trace('workflow-handler-set-state', hide_args=True)
def set_workflow_state(wf_ex, state, msg=None):
    if states.is_completed(state):
        stop_workflow(wf_ex, state, msg)
    elif states.is_paused(state):
        pause_workflow(wf_ex, msg)
    else:
        raise exc.MistralError(
            'Invalid workflow execution state [wf_ex_id=%s, wf_name=%s, '
            'state=%s]' % (wf_ex.id, wf_ex.name, state)
        )


def _get_integrity_check_key(wf_ex):
    return 'wfh_c_a_f_i-%s' % wf_ex.id


@profiler.trace(
    'workflow-handler-schedule-check-and-fix-integrity',
    hide_args=True
)
def _schedule_check_and_fix_integrity(wf_ex, delay=0):
    """Schedules workflow integrity check.

    :param wf_ex: Workflow execution.
    :param delay: Minimum amount of time before the check should be made.
    """

    if CONF.engine.execution_integrity_check_delay < 0:
        # Never check integrity if it's a negative value.
        return

    key = _get_integrity_check_key(wf_ex)

    scheduler.schedule_call(
        None,
        _CHECK_AND_FIX_INTEGRITY_PATH,
        delay,
        key=key,
        wf_ex_id=wf_ex.id
    )
