# Copyright 2023 OpenStack Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""recreate mistral metrics table

Revision ID: 044
Revises: 043
Create Date: 2024-03-22 12:02:23.374515

"""

# revision identifiers, used by Alembic.
revision = '044'
down_revision = '043'

from alembic import op
import sqlalchemy as sa

from mistral.services import maintenance
from mistral_lib import utils


def upgrade():

    op.drop_table('mistral_metrics')

    mistral_metrics_table = op.create_table(
        'mistral_metrics',
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('value', sa.String(length=255), nullable=False),
        sa.UniqueConstraint('name'),
    )

    op.bulk_insert(mistral_metrics_table,
                   [
                       {
                           'created_at': utils.utc_now_sec(),
                           'updated_at': utils.utc_now_sec(),
                           'id': 1,
                           'name': 'maintenance_status',
                           'value': maintenance.RUNNING
                       }
                   ]
                   )
