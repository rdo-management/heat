#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import copy
import uuid

from oslo_config import cfg
from oslo_log import log as logging
from oslo_serialization import jsonutils
from oslo_utils import netutils
from oslo_utils import uuidutils
import six

from heat.common import exception
from heat.common.i18n import _
from heat.common.i18n import _LI
from heat.engine import attributes
from heat.engine import constraints
from heat.engine import properties
from heat.engine import resource
from heat.engine.resources.openstack.neutron import subnet
from heat.engine.resources import stack_user
from heat.engine import scheduler
from heat.engine import support
from heat.rpc import api as rpc_api

cfg.CONF.import_opt('instance_user', 'heat.common.config')
cfg.CONF.import_opt('default_software_config_transport', 'heat.common.config')
cfg.CONF.import_opt('stack_scheduler_hints', 'heat.common.config')

LOG = logging.getLogger(__name__)


class Server(stack_user.StackUser):

    PROPERTIES = (
        NAME, IMAGE, BLOCK_DEVICE_MAPPING, BLOCK_DEVICE_MAPPING_V2,
        FLAVOR, FLAVOR_UPDATE_POLICY, IMAGE_UPDATE_POLICY, KEY_NAME,
        ADMIN_USER, AVAILABILITY_ZONE, SECURITY_GROUPS, NETWORKS,
        SCHEDULER_HINTS, METADATA, USER_DATA_FORMAT, USER_DATA,
        RESERVATION_ID, CONFIG_DRIVE, DISK_CONFIG, PERSONALITY,
        ADMIN_PASS, SOFTWARE_CONFIG_TRANSPORT
    ) = (
        'name', 'image', 'block_device_mapping', 'block_device_mapping_v2',
        'flavor', 'flavor_update_policy', 'image_update_policy', 'key_name',
        'admin_user', 'availability_zone', 'security_groups', 'networks',
        'scheduler_hints', 'metadata', 'user_data_format', 'user_data',
        'reservation_id', 'config_drive', 'diskConfig', 'personality',
        'admin_pass', 'software_config_transport'
    )

    _BLOCK_DEVICE_MAPPING_KEYS = (
        BLOCK_DEVICE_MAPPING_DEVICE_NAME, BLOCK_DEVICE_MAPPING_VOLUME_ID,
        BLOCK_DEVICE_MAPPING_SNAPSHOT_ID,
        BLOCK_DEVICE_MAPPING_VOLUME_SIZE,
        BLOCK_DEVICE_MAPPING_DELETE_ON_TERM,
    ) = (
        'device_name', 'volume_id',
        'snapshot_id',
        'volume_size',
        'delete_on_termination',
    )

    _BLOCK_DEVICE_MAPPING_V2_KEYS = (
        BLOCK_DEVICE_MAPPING_DEVICE_NAME,
        BLOCK_DEVICE_MAPPING_VOLUME_ID,
        BLOCK_DEVICE_MAPPING_IMAGE_ID,
        BLOCK_DEVICE_MAPPING_SNAPSHOT_ID,
        BLOCK_DEVICE_MAPPING_SWAP_SIZE,
        BLOCK_DEVICE_MAPPING_DEVICE_TYPE,
        BLOCK_DEVICE_MAPPING_DISK_BUS,
        BLOCK_DEVICE_MAPPING_BOOT_INDEX,
        BLOCK_DEVICE_MAPPING_VOLUME_SIZE,
        BLOCK_DEVICE_MAPPING_DELETE_ON_TERM,
    ) = (
        'device_name',
        'volume_id',
        'image_id',
        'snapshot_id',
        'swap_size',
        'device_type',
        'disk_bus',
        'boot_index',
        'volume_size',
        'delete_on_termination',
    )

    _NETWORK_KEYS = (
        NETWORK_UUID, NETWORK_ID, NETWORK_FIXED_IP, NETWORK_PORT,
    ) = (
        'uuid', 'network', 'fixed_ip', 'port',
    )

    _SOFTWARE_CONFIG_FORMATS = (
        HEAT_CFNTOOLS, RAW, SOFTWARE_CONFIG
    ) = (
        'HEAT_CFNTOOLS', 'RAW', 'SOFTWARE_CONFIG'
    )

    _SOFTWARE_CONFIG_TRANSPORTS = (
        POLL_SERVER_CFN, POLL_SERVER_HEAT, POLL_TEMP_URL
    ) = (
        'POLL_SERVER_CFN', 'POLL_SERVER_HEAT', 'POLL_TEMP_URL'
    )

    ATTRIBUTES = (
        NAME_ATTR, SHOW, ADDRESSES, NETWORKS_ATTR, FIRST_ADDRESS,
        INSTANCE_NAME, ACCESSIPV4, ACCESSIPV6, CONSOLE_URLS,
    ) = (
        'name', 'show', 'addresses', 'networks', 'first_address',
        'instance_name', 'accessIPv4', 'accessIPv6', 'console_urls',
    )
    properties_schema = {
        NAME: properties.Schema(
            properties.Schema.STRING,
            _('Server name.'),
            update_allowed=True
        ),
        IMAGE: properties.Schema(
            properties.Schema.STRING,
            _('The ID or name of the image to boot with.'),
            constraints=[
                constraints.CustomConstraint('glance.image')
            ],
            update_allowed=True
        ),
        BLOCK_DEVICE_MAPPING: properties.Schema(
            properties.Schema.LIST,
            _('Block device mappings for this server.'),
            schema=properties.Schema(
                properties.Schema.MAP,
                schema={
                    BLOCK_DEVICE_MAPPING_DEVICE_NAME: properties.Schema(
                        properties.Schema.STRING,
                        _('A device name where the volume will be '
                          'attached in the system at /dev/device_name. '
                          'This value is typically vda.'),
                        required=True
                    ),
                    BLOCK_DEVICE_MAPPING_VOLUME_ID: properties.Schema(
                        properties.Schema.STRING,
                        _('The ID of the volume to boot from. Only one '
                          'of volume_id or snapshot_id should be '
                          'provided.'),
                        constraints=[
                            constraints.CustomConstraint('cinder.volume')
                        ]
                    ),
                    BLOCK_DEVICE_MAPPING_SNAPSHOT_ID: properties.Schema(
                        properties.Schema.STRING,
                        _('The ID of the snapshot to create a volume '
                          'from.'),
                        constraints=[
                            constraints.CustomConstraint('cinder.snapshot')
                        ]
                    ),
                    BLOCK_DEVICE_MAPPING_VOLUME_SIZE: properties.Schema(
                        properties.Schema.INTEGER,
                        _('The size of the volume, in GB. It is safe to '
                          'leave this blank and have the Compute service '
                          'infer the size.')
                    ),
                    BLOCK_DEVICE_MAPPING_DELETE_ON_TERM: properties.Schema(
                        properties.Schema.BOOLEAN,
                        _('Indicate whether the volume should be deleted '
                          'when the server is terminated.')
                    ),
                },
            )
        ),
        BLOCK_DEVICE_MAPPING_V2: properties.Schema(
            properties.Schema.LIST,
            _('Block device mappings v2 for this server.'),
            schema=properties.Schema(
                properties.Schema.MAP,
                schema={
                    BLOCK_DEVICE_MAPPING_DEVICE_NAME: properties.Schema(
                        properties.Schema.STRING,
                        _('A device name where the volume will be '
                          'attached in the system at /dev/device_name. '
                          'This value is typically vda.'),
                    ),
                    BLOCK_DEVICE_MAPPING_VOLUME_ID: properties.Schema(
                        properties.Schema.STRING,
                        _('The volume_id can be boot or non-boot device '
                          'to the server.'),
                        constraints=[
                            constraints.CustomConstraint('cinder.volume')
                        ]
                    ),
                    BLOCK_DEVICE_MAPPING_IMAGE_ID: properties.Schema(
                        properties.Schema.STRING,
                        _('The ID of the image to create a volume from.'),
                        constraints=[
                            constraints.CustomConstraint('glance.image')
                        ],
                    ),
                    BLOCK_DEVICE_MAPPING_SNAPSHOT_ID: properties.Schema(
                        properties.Schema.STRING,
                        _('The ID of the snapshot to create a volume '
                          'from.'),
                        constraints=[
                            constraints.CustomConstraint('cinder.snapshot')
                        ]
                    ),
                    BLOCK_DEVICE_MAPPING_SWAP_SIZE: properties.Schema(
                        properties.Schema.INTEGER,
                        _('The size of the swap, in MB.')
                    ),
                    BLOCK_DEVICE_MAPPING_DEVICE_TYPE: properties.Schema(
                        properties.Schema.STRING,
                        _('Device type: at the moment we can make distinction'
                          ' only between disk and cdrom.'),
                        constraints=[
                            constraints.AllowedValues(['cdrom', 'disk']),
                        ],
                    ),
                    BLOCK_DEVICE_MAPPING_DISK_BUS: properties.Schema(
                        properties.Schema.STRING,
                        _('Bus of the device: hypervisor driver chooses a '
                          'suitable default if omitted.'),
                        constraints=[
                            constraints.AllowedValues(['ide', 'lame_bus',
                                                       'scsi', 'usb',
                                                       'virtio']),
                        ],
                    ),
                    BLOCK_DEVICE_MAPPING_BOOT_INDEX: properties.Schema(
                        properties.Schema.INTEGER,
                        _('Integer used for ordering the boot disks.'),
                    ),
                    BLOCK_DEVICE_MAPPING_VOLUME_SIZE: properties.Schema(
                        properties.Schema.INTEGER,
                        _('Size of the block device in GB. If it is omitted, '
                          'hypervisor driver calculates size.'),
                    ),
                    BLOCK_DEVICE_MAPPING_DELETE_ON_TERM: properties.Schema(
                        properties.Schema.BOOLEAN,
                        _('Indicate whether the volume should be deleted '
                          'when the server is terminated.')
                    ),
                },
            ),
            support_status=support.SupportStatus(version='2015.1')
        ),
        FLAVOR: properties.Schema(
            properties.Schema.STRING,
            _('The ID or name of the flavor to boot onto.'),
            required=True,
            update_allowed=True,
            constraints=[
                constraints.CustomConstraint('nova.flavor')
            ]
        ),
        FLAVOR_UPDATE_POLICY: properties.Schema(
            properties.Schema.STRING,
            _('Policy on how to apply a flavor update; either by requesting '
              'a server resize or by replacing the entire server.'),
            default='RESIZE',
            constraints=[
                constraints.AllowedValues(['RESIZE', 'REPLACE']),
            ],
            update_allowed=True
        ),
        IMAGE_UPDATE_POLICY: properties.Schema(
            properties.Schema.STRING,
            _('Policy on how to apply an image-id update; either by '
              'requesting a server rebuild or by replacing the entire server'),
            default='REBUILD',
            constraints=[
                constraints.AllowedValues(['REBUILD', 'REPLACE',
                                           'REBUILD_PRESERVE_EPHEMERAL']),
            ],
            update_allowed=True
        ),
        KEY_NAME: properties.Schema(
            properties.Schema.STRING,
            _('Name of keypair to inject into the server.'),
            constraints=[
                constraints.CustomConstraint('nova.keypair')
            ]
        ),
        ADMIN_USER: properties.Schema(
            properties.Schema.STRING,
            _('Name of the administrative user to use on the server. '
              'The default cloud-init user set up for each image '
              '(e.g. "ubuntu" for Ubuntu 12.04+, "fedora" for Fedora 19+ '
              'and "cloud-user" for CentOS/RHEL 6.5).'),
            support_status=support.SupportStatus(
                status=support.DEPRECATED,
                version='2014.1')
        ),
        AVAILABILITY_ZONE: properties.Schema(
            properties.Schema.STRING,
            _('Name of the availability zone for server placement.')
        ),
        SECURITY_GROUPS: properties.Schema(
            properties.Schema.LIST,
            _('List of security group names or IDs. Cannot be used if '
              'neutron ports are associated with this server; assign '
              'security groups to the ports instead.'),
            default=[]
        ),
        NETWORKS: properties.Schema(
            properties.Schema.LIST,
            _('An ordered list of nics to be added to this server, with '
              'information about connected networks, fixed ips, port etc.'),
            schema=properties.Schema(
                properties.Schema.MAP,
                schema={
                    NETWORK_UUID: properties.Schema(
                        properties.Schema.STRING,
                        _('ID of network to create a port on.'),
                        support_status=support.SupportStatus(
                            status=support.DEPRECATED,
                            message=_('Use property %s.') % NETWORK_ID,
                            version='2014.1'),
                        constraints=[
                            constraints.CustomConstraint('neutron.network')
                        ]
                    ),
                    NETWORK_ID: properties.Schema(
                        properties.Schema.STRING,
                        _('Name or ID of network to create a port on.'),
                        constraints=[
                            constraints.CustomConstraint('neutron.network')
                        ]
                    ),
                    NETWORK_FIXED_IP: properties.Schema(
                        properties.Schema.STRING,
                        _('Fixed IP address to specify for the port '
                          'created on the requested network.'),
                        constraints=[
                            constraints.CustomConstraint('ip_addr')
                        ]
                    ),
                    NETWORK_PORT: properties.Schema(
                        properties.Schema.STRING,
                        _('ID of an existing port to associate with this '
                          'server.'),
                        constraints=[
                            constraints.CustomConstraint('neutron.port')
                        ]
                    ),
                },
            ),
            update_allowed=True
        ),
        SCHEDULER_HINTS: properties.Schema(
            properties.Schema.MAP,
            _('Arbitrary key-value pairs specified by the client to help '
              'boot a server.')
        ),
        METADATA: properties.Schema(
            properties.Schema.MAP,
            _('Arbitrary key/value metadata to store for this server. Both '
              'keys and values must be 255 characters or less.  Non-string '
              'values will be serialized to JSON (and the serialized '
              'string must be 255 characters or less).'),
            update_allowed=True
        ),
        USER_DATA_FORMAT: properties.Schema(
            properties.Schema.STRING,
            _('How the user_data should be formatted for the server. For '
              'HEAT_CFNTOOLS, the user_data is bundled as part of the '
              'heat-cfntools cloud-init boot configuration data. For RAW '
              'the user_data is passed to Nova unmodified. '
              'For SOFTWARE_CONFIG user_data is bundled as part of the '
              'software config data, and metadata is derived from any '
              'associated SoftwareDeployment resources.'),
            default=HEAT_CFNTOOLS,
            constraints=[
                constraints.AllowedValues(_SOFTWARE_CONFIG_FORMATS),
            ]
        ),
        SOFTWARE_CONFIG_TRANSPORT: properties.Schema(
            properties.Schema.STRING,
            _('How the server should receive the metadata required for '
              'software configuration. POLL_SERVER_CFN will allow calls to '
              'the cfn API action DescribeStackResource authenticated with '
              'the provided keypair. POLL_SERVER_HEAT will allow calls to '
              'the Heat API resource-show using the provided keystone '
              'credentials. POLL_TEMP_URL will create and populate a '
              'Swift TempURL with metadata for polling.'),
            default=cfg.CONF.default_software_config_transport,
            constraints=[
                constraints.AllowedValues(_SOFTWARE_CONFIG_TRANSPORTS),
            ]
        ),
        USER_DATA: properties.Schema(
            properties.Schema.STRING,
            _('User data script to be executed by cloud-init.'),
            default=''
        ),
        RESERVATION_ID: properties.Schema(
            properties.Schema.STRING,
            _('A UUID for the set of servers being requested.')
        ),
        CONFIG_DRIVE: properties.Schema(
            properties.Schema.BOOLEAN,
            _('If True, enable config drive on the server.')
        ),
        DISK_CONFIG: properties.Schema(
            properties.Schema.STRING,
            _('Control how the disk is partitioned when the server is '
              'created.'),
            constraints=[
                constraints.AllowedValues(['AUTO', 'MANUAL']),
            ]
        ),
        PERSONALITY: properties.Schema(
            properties.Schema.MAP,
            _('A map of files to create/overwrite on the server upon boot. '
              'Keys are file names and values are the file contents.'),
            default={}
        ),
        ADMIN_PASS: properties.Schema(
            properties.Schema.STRING,
            _('The administrator password for the server.'),
            update_allowed=True
        ),
    }

    attributes_schema = {
        NAME_ATTR: attributes.Schema(
            _('Name of the server.'),
            type=attributes.Schema.STRING
        ),
        SHOW: attributes.Schema(
            _('A dict of all server details as returned by the API.'),
            type=attributes.Schema.MAP
        ),
        ADDRESSES: attributes.Schema(
            _('A dict of all network addresses with corresponding port_id. '
              'The port ID may be obtained through the following expression: '
              '"{get_attr: [<server>, addresses, <network name>, 0, port]}".'),
            type=attributes.Schema.MAP
        ),
        NETWORKS_ATTR: attributes.Schema(
            _('A dict of assigned network addresses of the form: '
              '{"public": [ip1, ip2...], "private": [ip3, ip4]}.'),
            type=attributes.Schema.MAP
        ),
        FIRST_ADDRESS: attributes.Schema(
            _('Convenience attribute to fetch the first assigned network '
              'address, or an empty string if nothing has been assigned at '
              'this time. Result may not be predictable if the server has '
              'addresses from more than one network.'),
            support_status=support.SupportStatus(
                status=support.DEPRECATED,
                version='2014.2',
                message=_('Use the networks attribute instead of '
                          'first_address. For example: "{get_attr: '
                          '[<server name>, networks, <network name>, 0]}"')
            )
        ),
        INSTANCE_NAME: attributes.Schema(
            _('AWS compatible instance name.'),
            type=attributes.Schema.STRING
        ),
        ACCESSIPV4: attributes.Schema(
            _('The manually assigned alternative public IPv4 address '
              'of the server.'),
            type=attributes.Schema.STRING
        ),
        ACCESSIPV6: attributes.Schema(
            _('The manually assigned alternative public IPv6 address '
              'of the server.'),
            type=attributes.Schema.STRING
        ),
        CONSOLE_URLS: attributes.Schema(
            _("URLs of server's consoles. "
              "To get a specific console type, the requested type "
              "can be specified as parameter to the get_attr function, "
              "e.g. get_attr: [ <server>, console_urls, novnc ]. "
              "Currently supported types are "
              "novnc, xvpvnc, spice-html5, rdp-html5, serial."),
            support_status=support.SupportStatus(version='2015.1'),
            type=attributes.Schema.MAP
        ),
    }

    # Server host name limit to 53 characters by due to typical default
    # linux HOST_NAME_MAX of 64, minus the .novalocal appended to the name
    physical_resource_name_limit = 53

    default_client_name = 'nova'

    def __init__(self, name, json_snippet, stack):
        super(Server, self).__init__(name, json_snippet, stack)
        if self.user_data_software_config():
            self._register_access_key()

    def _server_name(self):
        name = self.properties[self.NAME]
        if name:
            return name

        return self.physical_resource_name()

    def _config_drive(self):
        # This method is overridden by the derived CloudServer resource
        return self.properties[self.CONFIG_DRIVE]

    def _populate_deployments_metadata(self, meta):
        meta['deployments'] = meta.get('deployments', [])
        if self.transport_poll_server_heat():
            meta['os-collect-config'] = {'heat': {
                'user_id': self._get_user_id(),
                'password': self.password,
                'auth_url': self.context.auth_url,
                'project_id': self.stack.stack_user_project_id,
                'stack_id': self.stack.identifier().stack_path(),
                'resource_name': self.name}
            }
        elif self.transport_poll_server_cfn():
            meta['os-collect-config'] = {'cfn': {
                'metadata_url': '%s/v1/' % cfg.CONF.heat_metadata_server_url,
                'access_key_id': self.access_key,
                'secret_access_key': self.secret_key,
                'stack_name': self.stack.name,
                'path': '%s.Metadata' % self.name}
            }
        elif self.transport_poll_temp_url():
            container = self.physical_resource_name()
            object_name = str(uuid.uuid4())

            self.client('swift').put_container(container)

            url = self.client_plugin('swift').get_temp_url(
                container, object_name, method='GET')
            put_url = self.client_plugin('swift').get_temp_url(
                container, object_name)
            self.data_set('metadata_put_url', put_url)
            self.data_set('metadata_object_name', object_name)

            meta['os-collect-config'] = {'request': {
                'metadata_url': url}
            }
            self.client('swift').put_object(
                container, object_name, jsonutils.dumps(meta))
        self.metadata_set(meta)

    def _register_access_key(self):
        '''
        Access is limited to this resource, which created the keypair
        '''
        def access_allowed(resource_name):
            return resource_name == self.name

        if self.transport_poll_server_cfn():
            self.stack.register_access_allowed_handler(
                self.access_key, access_allowed)
        elif self.transport_poll_server_heat():
            self.stack.register_access_allowed_handler(
                self._get_user_id(), access_allowed)

    def _create_transport_credentials(self):
        if self.transport_poll_server_cfn():
            self._create_user()
            self._create_keypair()

        elif self.transport_poll_server_heat():
            self.password = uuid.uuid4().hex
            self._create_user()

        self._register_access_key()

    @property
    def access_key(self):
        return self.data().get('access_key')

    @property
    def secret_key(self):
        return self.data().get('secret_key')

    @property
    def password(self):
        return self.data().get('password')

    @password.setter
    def password(self, password):
        if password is None:
            self.data_delete('password')
        else:
            self.data_set('password', password, True)

    def user_data_raw(self):
        return self.properties[self.USER_DATA_FORMAT] == self.RAW

    def user_data_software_config(self):
        return self.properties[
            self.USER_DATA_FORMAT] == self.SOFTWARE_CONFIG

    def transport_poll_server_cfn(self):
        return self.properties[
            self.SOFTWARE_CONFIG_TRANSPORT] == self.POLL_SERVER_CFN

    def transport_poll_server_heat(self):
        return self.properties[
            self.SOFTWARE_CONFIG_TRANSPORT] == self.POLL_SERVER_HEAT

    def transport_poll_temp_url(self):
        return self.properties[
            self.SOFTWARE_CONFIG_TRANSPORT] == self.POLL_TEMP_URL

    def get_software_config(self, ud_content):
        try:
            sc = self.rpc_client().show_software_config(
                self.context, ud_content)
            return sc[rpc_api.SOFTWARE_CONFIG_CONFIG]
        except Exception as ex:
            self.rpc_client().ignore_error_named(ex, 'NotFound')
            return ud_content

    def handle_create(self):
        security_groups = self.properties[self.SECURITY_GROUPS]

        user_data_format = self.properties[self.USER_DATA_FORMAT]
        ud_content = self.properties[self.USER_DATA]
        if self.user_data_software_config() or self.user_data_raw():
            if uuidutils.is_uuid_like(ud_content):
                # attempt to load the userdata from software config
                ud_content = self.get_software_config(ud_content)

        metadata = self.metadata_get(True) or {}

        if self.user_data_software_config():
            self._create_transport_credentials()
            self._populate_deployments_metadata(metadata)

        if self.properties[self.ADMIN_USER]:
            instance_user = self.properties[self.ADMIN_USER]
        elif cfg.CONF.instance_user:
            instance_user = cfg.CONF.instance_user
        else:
            instance_user = None

        userdata = self.client_plugin().build_userdata(
            metadata,
            ud_content,
            instance_user=instance_user,
            user_data_format=user_data_format)

        flavor = self.properties[self.FLAVOR]
        availability_zone = self.properties[self.AVAILABILITY_ZONE]

        image = self.properties[self.IMAGE]
        if image:
            image = self.client_plugin('glance').get_image_id(image)

        flavor_id = self.client_plugin().get_flavor_id(flavor)

        instance_meta = self.properties[self.METADATA]
        if instance_meta is not None:
            instance_meta = self.client_plugin().meta_serialize(
                instance_meta)

        scheduler_hints = self.properties[self.SCHEDULER_HINTS]
        if cfg.CONF.stack_scheduler_hints:
            if scheduler_hints is None:
                scheduler_hints = {}
            scheduler_hints['heat_root_stack_id'] = self.stack.root_stack_id()
            scheduler_hints['heat_stack_id'] = self.stack.id
            scheduler_hints['heat_stack_name'] = self.stack.name
            scheduler_hints['heat_path_in_stack'] = self.stack.path_in_stack()
            scheduler_hints['heat_resource_name'] = self.name
        nics = self._build_nics(self.properties[self.NETWORKS])
        block_device_mapping = self._build_block_device_mapping(
            self.properties[self.BLOCK_DEVICE_MAPPING])
        block_device_mapping_v2 = self._build_block_device_mapping_v2(
            self.properties[self.BLOCK_DEVICE_MAPPING_V2])
        reservation_id = self.properties[self.RESERVATION_ID]
        disk_config = self.properties[self.DISK_CONFIG]
        admin_pass = self.properties[self.ADMIN_PASS] or None
        personality_files = self.properties[self.PERSONALITY]
        key_name = self.properties[self.KEY_NAME]

        server = None
        try:
            server = self.nova().servers.create(
                name=self._server_name(),
                image=image,
                flavor=flavor_id,
                key_name=key_name,
                security_groups=security_groups,
                userdata=userdata,
                meta=instance_meta,
                scheduler_hints=scheduler_hints,
                nics=nics,
                availability_zone=availability_zone,
                block_device_mapping=block_device_mapping,
                block_device_mapping_v2=block_device_mapping_v2,
                reservation_id=reservation_id,
                config_drive=self._config_drive(),
                disk_config=disk_config,
                files=personality_files,
                admin_pass=admin_pass)
        finally:
            # Avoid a race condition where the thread could be canceled
            # before the ID is stored
            if server is not None:
                self.resource_id_set(server.id)

        return server

    def check_create_complete(self, server):
        return self._check_active(server)

    def _check_active(self, server):
        cp = self.client_plugin()
        status = cp.get_status(server)
        if status != 'ACTIVE':
            cp.refresh_server(server)
            status = cp.get_status(server)

        if status in cp.deferred_server_statuses:
            return False
        elif status == 'ACTIVE':
            return True
        elif status == 'ERROR':
            fault = getattr(server, 'fault', {})
            raise resource.ResourceInError(
                resource_status=status,
                status_reason=_("Message: %(message)s, Code: %(code)s") % {
                    'message': fault.get('message', _('Unknown')),
                    'code': fault.get('code', _('Unknown'))
                })
        else:
            raise resource.ResourceUnknownStatus(
                resource_status=server.status,
                result=_('Server is not active'))

    def _check_server_status(self):
        server = self.nova().servers.get(self.resource_id)
        status = self.client_plugin().get_status(server)
        checks = [{'attr': 'status', 'expected': 'ACTIVE', 'current': status}]
        self._verify_check_conditions(checks)
        return server

    def handle_check(self):
        self._check_server_status()

    @classmethod
    def _build_block_device_mapping(cls, bdm):
        if not bdm:
            return None
        bdm_dict = {}
        for mapping in bdm:
            mapping_parts = []
            snapshot_id = mapping.get(cls.BLOCK_DEVICE_MAPPING_SNAPSHOT_ID)
            if snapshot_id:
                mapping_parts.append(snapshot_id)
                mapping_parts.append('snap')
            else:
                volume_id = mapping.get(cls.BLOCK_DEVICE_MAPPING_VOLUME_ID)
                mapping_parts.append(volume_id)
                mapping_parts.append('')

            volume_size = mapping.get(cls.BLOCK_DEVICE_MAPPING_VOLUME_SIZE)
            delete = mapping.get(cls.BLOCK_DEVICE_MAPPING_DELETE_ON_TERM)
            if volume_size:
                mapping_parts.append(str(volume_size))
            else:
                mapping_parts.append('')
            if delete:
                mapping_parts.append(str(delete))

            device_name = mapping.get(cls.BLOCK_DEVICE_MAPPING_DEVICE_NAME)
            bdm_dict[device_name] = ':'.join(mapping_parts)

        return bdm_dict

    @classmethod
    def _build_block_device_mapping_v2(cls, bdm_v2):
        if not bdm_v2:
            return None

        bdm_v2_list = []
        for mapping in bdm_v2:
            bmd_dict = None
            if mapping.get(cls.BLOCK_DEVICE_MAPPING_VOLUME_ID):
                bmd_dict = {
                    'uuid': mapping.get(cls.BLOCK_DEVICE_MAPPING_VOLUME_ID),
                    'source_type': 'volume',
                    'destination_type': 'volume',
                    'boot_index': 0,
                    'delete_on_termination': False,
                }
            elif mapping.get(cls.BLOCK_DEVICE_MAPPING_SNAPSHOT_ID):
                bmd_dict = {
                    'uuid': mapping.get(cls.BLOCK_DEVICE_MAPPING_SNAPSHOT_ID),
                    'source_type': 'snapshot',
                    'destination_type': 'volume',
                    'boot_index': 0,
                    'delete_on_termination': False,
                }
            elif mapping.get(cls.BLOCK_DEVICE_MAPPING_IMAGE_ID):
                bmd_dict = {
                    'uuid': mapping.get(cls.BLOCK_DEVICE_MAPPING_IMAGE_ID),
                    'source_type': 'image',
                    'destination_type': 'volume',
                    'boot_index': 0,
                    'delete_on_termination': False,
                }
            elif mapping.get(cls.BLOCK_DEVICE_MAPPING_SWAP_SIZE):
                bmd_dict = {
                    'source_type': 'blank',
                    'destination_type': 'local',
                    'boot_index': -1,
                    'delete_on_termination': True,
                    'guest_format': 'swap',
                    'volume_size': mapping.get(
                        cls.BLOCK_DEVICE_MAPPING_SWAP_SIZE),
                }

            update_props = (cls.BLOCK_DEVICE_MAPPING_DEVICE_NAME,
                            cls.BLOCK_DEVICE_MAPPING_DEVICE_TYPE,
                            cls.BLOCK_DEVICE_MAPPING_DISK_BUS,
                            cls.BLOCK_DEVICE_MAPPING_BOOT_INDEX,
                            cls.BLOCK_DEVICE_MAPPING_VOLUME_SIZE,
                            cls.BLOCK_DEVICE_MAPPING_DELETE_ON_TERM)

            for update_prop in update_props:
                if mapping.get(update_prop) is not None:
                    bmd_dict[update_prop] = mapping.get(update_prop)

            if bmd_dict:
                bdm_v2_list.append(bmd_dict)

        return bdm_v2_list

    def _build_nics(self, networks):
        if not networks:
            return None

        nics = []

        for net_data in networks:
            nic_info = {}
            net_identifier = (net_data.get(self.NETWORK_UUID) or
                              net_data.get(self.NETWORK_ID))
            if net_identifier:
                if self.is_using_neutron():
                    net_id = (self.client_plugin(
                        'neutron').resolve_network(
                        net_data, self.NETWORK_ID, self.NETWORK_UUID))
                else:
                    net_id = (self.client_plugin(
                        'nova').get_nova_network_id(net_identifier))
                nic_info['net-id'] = net_id
            if net_data.get(self.NETWORK_FIXED_IP):
                ip = net_data[self.NETWORK_FIXED_IP]
                if netutils.is_valid_ipv6(ip):
                    nic_info['v6-fixed-ip'] = ip
                else:
                    nic_info['v4-fixed-ip'] = ip
            if net_data.get(self.NETWORK_PORT):
                nic_info['port-id'] = net_data[self.NETWORK_PORT]
            nics.append(nic_info)
        return nics

    def _add_port_for_address(self, server):
        nets = copy.deepcopy(server.addresses)
        ifaces = server.interface_list()
        ip_mac_mapping_on_port_id = dict(((iface.fixed_ips[0]['ip_address'],
                                           iface.mac_addr), iface.port_id)
                                         for iface in ifaces)
        for net_name in nets:
            for addr in nets[net_name]:
                addr['port'] = ip_mac_mapping_on_port_id.get(
                    (addr['addr'], addr['OS-EXT-IPS-MAC:mac_addr']))
        return nets

    def _resolve_attribute(self, name):
        if name == self.FIRST_ADDRESS:
            return self.client_plugin().server_to_ipaddress(
                self.resource_id) or ''
        if name == self.NAME_ATTR:
            return self._server_name()
        try:
            server = self.nova().servers.get(self.resource_id)
        except Exception as e:
            self.client_plugin().ignore_not_found(e)
            return ''
        if name == self.ADDRESSES:
            return self._add_port_for_address(server)
        if name == self.NETWORKS_ATTR:
            return server.networks
        if name == self.INSTANCE_NAME:
            return server._info.get('OS-EXT-SRV-ATTR:instance_name')
        if name == self.ACCESSIPV4:
            return server.accessIPv4
        if name == self.ACCESSIPV6:
            return server.accessIPv6
        if name == self.SHOW:
            return server._info
        if name == self.CONSOLE_URLS:
            return self.client_plugin('nova').get_console_urls(server)

    def add_dependencies(self, deps):
        super(Server, self).add_dependencies(deps)
        # Depend on any Subnet in this template with the same
        # network_id as the networks attached to this server.
        # It is not known which subnet a server might be assigned
        # to so all subnets in a network should be created before
        # the servers in that network.
        nets = self.properties[self.NETWORKS]
        if not nets:
            return
        for res in six.itervalues(self.stack):
            if res.has_interface('OS::Neutron::Subnet'):
                subnet_net = (res.properties.get(subnet.Subnet.NETWORK_ID)
                              or res.properties.get(subnet.Subnet.NETWORK))
                for net in nets:
                    # worry about network_id because that could be the match
                    # assigned to the subnet as well and could have been
                    # created by this stack. Regardless, the server should
                    # still wait on the subnet.
                    net_id = (net.get(self.NETWORK_ID) or
                              net.get(self.NETWORK_UUID))
                    if net_id and net_id == subnet_net:
                        deps += (self, res)
                        break

    def _get_network_matches(self, old_networks, new_networks):
        # make new_networks similar on old_networks
        for new_net in new_networks:
            for key in ('port', 'network', 'fixed_ip', 'uuid'):
                # if new_net.get(key) is '', convert to None
                if not new_net.get(key):
                    new_net[key] = None
        for old_net in old_networks:
            for key in ('port', 'network', 'fixed_ip', 'uuid'):
                # if old_net.get(key) is '', convert to None
                if not old_net.get(key):
                    old_net[key] = None
        # find matches and remove them from old and new networks
        not_updated_networks = []
        for net in old_networks:
            if net in new_networks:
                new_networks.remove(net)
                not_updated_networks.append(net)
        for net in not_updated_networks:
            old_networks.remove(net)
        return not_updated_networks

    def _get_network_id(self, net):
        net_id = None
        if net.get(self.NETWORK_ID):
            if self.is_using_neutron():
                net_id = self.client_plugin(
                    'neutron').resolve_network(
                    net,
                    self.NETWORK_ID, self.NETWORK_UUID)
            else:
                net_id = self.client_plugin(
                    'nova').get_nova_network_id(net.get(self.NETWORK_ID))
        return net_id

    def update_networks_matching_iface_port(self, nets, interfaces):

        def find_equal(port, net_id, ip, nets):
            for net in nets:

                if (net.get('port') == port or
                        (net.get('fixed_ip') == ip and
                         (self._get_network_id(net) == net_id or
                          net.get('uuid') == net_id))):
                    return net

        def find_poor_net(net_id, nets):
            for net in nets:
                if (not net.get('port') and not net.get('fixed_ip') and
                        (self._get_network_id(net) == net_id or
                         net.get('uuid') == net_id)):
                    return net

        for iface in interfaces:
            # get interface properties
            props = {'port': iface.port_id,
                     'net_id': iface.net_id,
                     'ip': iface.fixed_ips[0]['ip_address'],
                     'nets': nets}
            # try to match by port or network_id with fixed_ip
            net = find_equal(**props)
            if net is not None:
                net['port'] = props['port']
                continue
            # find poor net that has only network_id
            net = find_poor_net(props['net_id'], nets)
            if net is not None:
                net['port'] = props['port']

    def _update_flavor(self, server, prop_diff):
        flavor_update_policy = (
            prop_diff.get(self.FLAVOR_UPDATE_POLICY) or
            self.properties[self.FLAVOR_UPDATE_POLICY])
        flavor = prop_diff[self.FLAVOR]

        if flavor_update_policy == 'REPLACE':
            raise resource.UpdateReplace(self.name)

        flavor_id = self.client_plugin().get_flavor_id(flavor)
        if not server:
            server = self.nova().servers.get(self.resource_id)
        return scheduler.TaskRunner(self.client_plugin().resize,
                                    server, flavor, flavor_id)

    def _update_image(self, server, prop_diff):
        image_update_policy = (
            prop_diff.get(self.IMAGE_UPDATE_POLICY) or
            self.properties[self.IMAGE_UPDATE_POLICY])
        if image_update_policy == 'REPLACE':
            raise resource.UpdateReplace(self.name)
        image = prop_diff[self.IMAGE]
        image_id = self.client_plugin('glance').get_image_id(image)
        if not server:
            server = self.nova().servers.get(self.resource_id)
        preserve_ephemeral = (
            image_update_policy == 'REBUILD_PRESERVE_EPHEMERAL')
        password = (prop_diff.get(self.ADMIN_PASS) or
                    self.properties[self.ADMIN_PASS])
        return scheduler.TaskRunner(
            self.client_plugin().rebuild, server, image_id,
            password=password,
            preserve_ephemeral=preserve_ephemeral)

    def _update_networks(self, server, prop_diff):
        checkers = []
        new_networks = prop_diff.get(self.NETWORKS)
        attach_first_free_port = False
        if not new_networks:
            new_networks = []
            attach_first_free_port = True
        old_networks = self.properties[self.NETWORKS]

        if not server:
            server = self.nova().servers.get(self.resource_id)
        interfaces = server.interface_list()

        # if old networks is None, it means that the server got first
        # free port. so we should detach this interface.
        if old_networks is None:
            for iface in interfaces:
                checker = scheduler.TaskRunner(server.interface_detach,
                                               iface.port_id)
                checkers.append(checker)

        # if we have any information in networks field, we should:
        # 1. find similar networks, if they exist
        # 2. remove these networks from new_networks and old_networks
        #    lists
        # 3. detach unmatched networks, which were present in old_networks
        # 4. attach unmatched networks, which were present in new_networks
        else:
            # remove not updated networks from old and new networks lists,
            # also get list these networks
            not_updated_networks = self._get_network_matches(
                old_networks, new_networks)

            self.update_networks_matching_iface_port(
                old_networks + not_updated_networks, interfaces)

            # according to nova interface-detach command detached port
            # will be deleted
            for net in old_networks:
                if net.get(self.NETWORK_PORT):
                    checker = scheduler.TaskRunner(server.interface_detach,
                                                   net.get(self.NETWORK_PORT))
                    checkers.append(checker)

        # attach section similar for both variants that
        # were mentioned above

        for net in new_networks:
            if net.get(self.NETWORK_PORT):
                checker = scheduler.TaskRunner(server.interface_attach,
                                               net.get(self.NETWORK_PORT),
                                               None, None)
                checkers.append(checker)
            elif net.get(self.NETWORK_ID):
                checker = scheduler.TaskRunner(server.interface_attach,
                                               None, self._get_network_id(net),
                                               net.get('fixed_ip'))
                checkers.append(checker)
            elif net.get('uuid'):
                checker = scheduler.TaskRunner(server.interface_attach,
                                               None, net['uuid'],
                                               net.get('fixed_ip'))
                checkers.append(checker)

        # if new_networks is None, we should attach first free port,
        # according to similar behavior during instance creation
        if attach_first_free_port:
            checker = scheduler.TaskRunner(server.interface_attach,
                                           None, None, None)
            checkers.append(checker)

        return checkers

    def handle_update(self, json_snippet, tmpl_diff, prop_diff):
        if 'Metadata' in tmpl_diff:
            self.metadata_set(tmpl_diff['Metadata'])

        checkers = []
        server = None

        if self.METADATA in prop_diff:
            server = self.nova().servers.get(self.resource_id)
            self.client_plugin().meta_update(server,
                                             prop_diff[self.METADATA])

        if self.FLAVOR in prop_diff:
            checkers.append(self._update_flavor(server, prop_diff))

        if self.IMAGE in prop_diff:
            checkers.append(self._update_image(server, prop_diff))
        elif self.ADMIN_PASS in prop_diff:
            if not server:
                server = self.nova().servers.get(self.resource_id)
            server.change_password(prop_diff[self.ADMIN_PASS])

        if self.NAME in prop_diff:
            if not server:
                server = self.nova().servers.get(self.resource_id)
            self.client_plugin().rename(server, prop_diff[self.NAME])

        if self.NETWORKS in prop_diff:
            checkers.extend(self._update_networks(server, prop_diff))

        # Optimization: make sure the first task is started before
        # check_update_complete.
        if checkers:
            checkers[0].start()

        return checkers

    def check_update_complete(self, checkers):
        '''Push all checkers to completion in list order.'''
        for checker in checkers:
            if not checker.started():
                checker.start()
            if not checker.step():
                return False
        return True

    def metadata_update(self, new_metadata=None):
        '''
        Refresh the metadata if new_metadata is None
        '''
        if new_metadata is None:
            # Re-resolve the template metadata and merge it with the
            # current resource metadata.  This is necessary because the
            # attributes referenced in the template metadata may change
            # and the resource itself adds keys to the metadata which
            # are not specified in the template (e.g the deployments data)
            meta = self.metadata_get(refresh=True) or {}
            tmpl_meta = self.t.metadata()
            meta.update(tmpl_meta)
            self.metadata_set(meta)

    @staticmethod
    def _check_maximum(count, maximum, msg):
        '''
        Check a count against a maximum, unless maximum is -1 which indicates
        that there is no limit
        '''
        if maximum != -1 and count > maximum:
            raise exception.StackValidationFailed(message=msg)

    def _validate_block_device_mapping(self):

        # either volume_id or snapshot_id needs to be specified, but not both
        # for block device mapping.
        bdm = self.properties[self.BLOCK_DEVICE_MAPPING] or []
        bootable_vol = False
        for mapping in bdm:
            device_name = mapping[self.BLOCK_DEVICE_MAPPING_DEVICE_NAME]
            if device_name == 'vda':
                bootable_vol = True

            volume_id = mapping.get(self.BLOCK_DEVICE_MAPPING_VOLUME_ID)
            snapshot_id = mapping.get(self.BLOCK_DEVICE_MAPPING_SNAPSHOT_ID)
            if volume_id and snapshot_id:
                raise exception.ResourcePropertyConflict(
                    self.BLOCK_DEVICE_MAPPING_VOLUME_ID,
                    self.BLOCK_DEVICE_MAPPING_SNAPSHOT_ID)
            if not volume_id and not snapshot_id:
                msg = _('Either volume_id or snapshot_id must be specified for'
                        ' device mapping %s') % device_name
                raise exception.StackValidationFailed(message=msg)

        bdm_v2 = self.properties[self.BLOCK_DEVICE_MAPPING_V2] or []
        if bdm and bdm_v2:
            raise exception.ResourcePropertyConflict(
                self.BLOCK_DEVICE_MAPPING, self.BLOCK_DEVICE_MAPPING_V2)

        for mapping in bdm_v2:
            volume_id = mapping.get(self.BLOCK_DEVICE_MAPPING_VOLUME_ID)
            snapshot_id = mapping.get(self.BLOCK_DEVICE_MAPPING_SNAPSHOT_ID)
            image_id = mapping.get(self.BLOCK_DEVICE_MAPPING_IMAGE_ID)
            swap_size = mapping.get(self.BLOCK_DEVICE_MAPPING_SWAP_SIZE)

            property_tuple = (volume_id, snapshot_id, image_id, swap_size)

            if property_tuple.count(None) < 3:
                raise exception.ResourcePropertyConflict(
                    self.BLOCK_DEVICE_MAPPING_VOLUME_ID,
                    self.BLOCK_DEVICE_MAPPING_SNAPSHOT_ID,
                    self.BLOCK_DEVICE_MAPPING_IMAGE_ID,
                    self.BLOCK_DEVICE_MAPPING_SWAP_SIZE)

            if property_tuple.count(None) == 4:
                msg = _('Either volume_id, snapshot_id, image_id or '
                        'swap_size must be specified.')
                raise exception.StackValidationFailed(message=msg)

            if any((volume_id, snapshot_id, image_id)):
                bootable_vol = True

        return bootable_vol

    def validate(self):
        '''
        Validate any of the provided params
        '''
        super(Server, self).validate()

        bootable_vol = self._validate_block_device_mapping()

        # make sure the image exists if specified.
        image = self.properties[self.IMAGE]
        if not image and not bootable_vol:
            msg = _('Neither image nor bootable volume is specified for'
                    ' instance %s') % self.name
            raise exception.StackValidationFailed(message=msg)

        # network properties 'uuid' and 'network' shouldn't be used
        # both at once for all networks
        networks = self.properties[self.NETWORKS] or []
        # record if any networks include explicit ports
        networks_with_port = False
        for network in networks:
            networks_with_port = (networks_with_port or
                                  network.get(self.NETWORK_PORT))
            if not (network.get(self.NETWORK_ID)
                    or network.get(self.NETWORK_PORT)
                    or network.get(self.NETWORK_UUID)):
                msg = _('One of the properties "%(id)s", "%(port_id)s", '
                        '"%(uuid)s" should be set for the '
                        'specified network of server "%(server)s".'
                        '') % dict(id=self.NETWORK_ID,
                                   port_id=self.NETWORK_PORT,
                                   uuid=self.NETWORK_UUID,
                                   server=self.name)
                raise exception.StackValidationFailed(message=msg)

            if network.get(self.NETWORK_UUID) and network.get(self.NETWORK_ID):
                msg = _('Properties "%(uuid)s" and "%(id)s" are both set '
                        'to the network "%(network)s" for the server '
                        '"%(server)s". The "%(uuid)s" property is deprecated. '
                        'Use only "%(id)s" property.'
                        '') % dict(uuid=self.NETWORK_UUID,
                                   id=self.NETWORK_ID,
                                   network=network[self.NETWORK_ID],
                                   server=self.name)
                raise exception.StackValidationFailed(message=msg)
            elif network.get(self.NETWORK_UUID):
                LOG.info(_LI('For the server "%(server)s" the "%(uuid)s" '
                             'property is set to network "%(network)s". '
                             '"%(uuid)s" property is deprecated. Use '
                             '"%(id)s"  property instead.'),
                         dict(uuid=self.NETWORK_UUID,
                              id=self.NETWORK_ID,
                              network=network[self.NETWORK_ID],
                              server=self.name))

        # retrieve provider's absolute limits if it will be needed
        metadata = self.properties[self.METADATA]
        personality = self.properties[self.PERSONALITY]
        if metadata is not None or personality:
            limits = self.client_plugin().absolute_limits()

        # if 'security_groups' present for the server and explict 'port'
        # in one or more entries in 'networks', raise validation error
        if networks_with_port and self.properties[self.SECURITY_GROUPS]:
            raise exception.ResourcePropertyConflict(
                self.SECURITY_GROUPS,
                "/".join([self.NETWORKS, self.NETWORK_PORT]))

        # verify that the number of metadata entries is not greater
        # than the maximum number allowed in the provider's absolute
        # limits
        if metadata is not None:
            msg = _('Instance metadata must not contain greater than %s '
                    'entries.  This is the maximum number allowed by your '
                    'service provider') % limits['maxServerMeta']
            self._check_maximum(len(metadata),
                                limits['maxServerMeta'], msg)

        # verify the number of personality files and the size of each
        # personality file against the provider's absolute limits
        if personality:
            msg = _("The personality property may not contain "
                    "greater than %s entries.") % limits['maxPersonality']
            self._check_maximum(len(personality),
                                limits['maxPersonality'], msg)

            for path, contents in personality.items():
                msg = (_("The contents of personality file \"%(path)s\" "
                         "is larger than the maximum allowed personality "
                         "file size (%(max_size)s bytes).") %
                       {'path': path,
                        'max_size': limits['maxPersonalitySize']})
                self._check_maximum(len(bytes(contents)),
                                    limits['maxPersonalitySize'], msg)

    def _delete_temp_url(self):
        object_name = self.data().get('metadata_object_name')
        if not object_name:
            return
        try:
            container = self.physical_resource_name()
            swift = self.client('swift')
            swift.delete_object(container, object_name)
            headers = swift.head_container(container)
            if int(headers['x-container-object-count']) == 0:
                swift.delete_container(container)
        except Exception as ex:
            self.client_plugin('swift').ignore_not_found(ex)

    def handle_delete(self):

        if self.resource_id is None:
            return

        if self.user_data_software_config():
            self._delete_user()
            self._delete_temp_url()

        try:
            server = self.nova().servers.get(self.resource_id)
        except Exception as e:
            self.client_plugin().ignore_not_found(e)
        else:
            deleter = scheduler.TaskRunner(self.client_plugin().delete_server,
                                           server)
            deleter.start()
            return deleter

    def check_delete_complete(self, deleter):
        if deleter is None or deleter.step():
            return True
        return False

    def handle_suspend(self):
        '''
        Suspend a server - note we do not wait for the SUSPENDED state,
        this is polled for by check_suspend_complete in a similar way to the
        create logic so we can take advantage of coroutines
        '''
        if self.resource_id is None:
            raise exception.Error(_('Cannot suspend %s, resource_id not set') %
                                  self.name)

        try:
            server = self.nova().servers.get(self.resource_id)
        except Exception as e:
            if self.client_plugin().is_not_found(e):
                raise exception.NotFound(_('Failed to find server %s') %
                                         self.resource_id)
            else:
                raise
        else:
            LOG.debug('suspending server %s' % self.resource_id)
            # We want the server.suspend to happen after the volume
            # detachement has finished, so pass both tasks and the server
            suspend_runner = scheduler.TaskRunner(server.suspend)
            return server, suspend_runner

    def check_suspend_complete(self, cookie):
        server, suspend_runner = cookie

        if not suspend_runner.started():
            suspend_runner.start()

        if suspend_runner.done():
            if server.status == 'SUSPENDED':
                return True

            cp = self.client_plugin()
            cp.refresh_server(server)
            LOG.debug('%(name)s check_suspend_complete status = %(status)s'
                      % {'name': self.name, 'status': server.status})
            if server.status in list(cp.deferred_server_statuses +
                                     ['ACTIVE']):
                return server.status == 'SUSPENDED'
            else:
                exc = exception.Error(_('Suspend of server %(server)s failed '
                                        'with unknown status: %(status)s') %
                                      dict(server=server.name,
                                           status=server.status))
                raise exc

    def handle_resume(self):
        '''
        Resume a server - note we do not wait for the ACTIVE state,
        this is polled for by check_resume_complete in a similar way to the
        create logic so we can take advantage of coroutines
        '''
        if self.resource_id is None:
            raise exception.Error(_('Cannot resume %s, resource_id not set') %
                                  self.name)

        try:
            server = self.nova().servers.get(self.resource_id)
        except Exception as e:
            if self.client_plugin().is_not_found(e):
                raise exception.NotFound(_('Failed to find server %s') %
                                         self.resource_id)
            else:
                raise
        else:
            LOG.debug('resuming server %s' % self.resource_id)
            server.resume()
            return server

    def check_resume_complete(self, server):
        return self._check_active(server)

    def handle_snapshot(self):
        image_id = self.nova().servers.create_image(
            self.resource_id, self.physical_resource_name())
        self.data_set('snapshot_image_id', image_id)
        return image_id

    def check_snapshot_complete(self, image_id):
        image = self.nova().images.get(image_id)
        if image.status == 'ACTIVE':
            return True
        elif image.status == 'ERROR' or image.status == 'DELETED':
            raise exception.Error(image.status)

        return False

    def handle_delete_snapshot(self, snapshot):
        image_id = snapshot['resource_data'].get('snapshot_image_id')
        try:
            self.nova().images.delete(image_id)
        except Exception as e:
            self.client_plugin().ignore_not_found(e)

    def handle_restore(self, defn, restore_data):
        image_id = restore_data['resource_data']['snapshot_image_id']
        props = dict(
            (key, value) for (key, value) in
            six.iteritems(defn.properties(self.properties_schema))
            if value is not None)
        props[self.IMAGE] = image_id
        return defn.freeze(properties=props)


def resource_mapping():
    return {
        'OS::Nova::Server': Server,
    }
