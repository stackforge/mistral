===========
policy.yaml
===========

.. warning::

   JSON formatted policy file is deprecated since Mistral 12.0.0 (Wallaby).
   This `oslopolicy-convert-json-to-yaml`__ tool will migrate your existing
   JSON-formatted policy file to YAML in a backward-compatible way.

.. __: https://docs.openstack.org/oslo.policy/latest/cli/oslopolicy-convert-json-to-yaml.html

Use the ``policy.yaml`` file to define additional access controls that apply to
the Mistral services:

.. literalinclude:: /_static/mistral.policy.yaml.sample

