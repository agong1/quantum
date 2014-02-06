# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright (c) 2012 OpenStack Foundation.
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

import contextlib
import sys

import mock
from oslo.config import cfg
import testtools

from neutron.agent.linux import ip_lib
from neutron.agent.linux import ovs_lib
from neutron.agent.linux import utils
from neutron.common import constants as n_const
from neutron.openstack.common import importutils
from neutron.openstack.common.rpc import common as rpc_common
from neutron.plugins.common import constants as p_const
from neutron.plugins.ofswitch.common import constants
from neutron.tests import base
from neutron.tests.unit.ofswitch import fake_ryu


NOTIFIER = ('neutron.plugins.ml2.rpc.AgentNotifierApi')


class OFSAgentTestCase(base.BaseTestCase):

    _AGENT_NAME = 'neutron.plugins.ofswitch.agent.ofs_neutron_agent'

    def setUp(self):
        super(OFSAgentTestCase, self).setUp()
        self.addCleanup(mock.patch.stopall)
        self.fake_ryu_of = fake_ryu.patch_fake_ryu_of().start()
        self.mod_agent = importutils.import_module(self._AGENT_NAME)


class CreateAgentConfigMap(OFSAgentTestCase):

    def test_create_agent_config_map_succeeds(self):
        self.assertTrue(self.mod_agent.create_agent_config_map(cfg.CONF))

    def test_create_agent_config_map_fails_for_invalid_tunnel_config(self):
        self.addCleanup(cfg.CONF.reset)
        # An ip address is required for tunneling but there is no default,
        # verify this for both gre and vxlan tunnels.
        cfg.CONF.set_override('tunnel_types', [p_const.TYPE_GRE],
                              group='AGENT')
        with testtools.ExpectedException(ValueError):
            self.mod_agent.create_agent_config_map(cfg.CONF)
        cfg.CONF.set_override('tunnel_types', [p_const.TYPE_VXLAN],
                              group='AGENT')
        with testtools.ExpectedException(ValueError):
            self.mod_agent.create_agent_config_map(cfg.CONF)

    def test_create_agent_config_map_enable_tunneling(self):
        self.addCleanup(cfg.CONF.reset)
        # Verify setting only enable_tunneling will default tunnel_type to GRE
        cfg.CONF.set_override('tunnel_types', None, group='AGENT')
        cfg.CONF.set_override('enable_tunneling', True, group='OFS')
        cfg.CONF.set_override('local_ip', '10.10.10.10', group='OFS')
        cfgmap = self.mod_agent.create_agent_config_map(cfg.CONF)
        self.assertEqual(cfgmap['tunnel_types'], [p_const.TYPE_GRE])

    def test_create_agent_config_map_fails_no_local_ip(self):
        self.addCleanup(cfg.CONF.reset)
        # An ip address is required for tunneling but there is no default
        cfg.CONF.set_override('enable_tunneling', True, group='OFS')
        with testtools.ExpectedException(ValueError):
            self.mod_agent.create_agent_config_map(cfg.CONF)

    def test_create_agent_config_map_fails_for_invalid_tunnel_type(self):
        self.addCleanup(cfg.CONF.reset)
        cfg.CONF.set_override('tunnel_types', ['foobar'], group='AGENT')
        with testtools.ExpectedException(ValueError):
            self.mod_agent.create_agent_config_map(cfg.CONF)

    def test_create_agent_config_map_multiple_tunnel_types(self):
        self.addCleanup(cfg.CONF.reset)
        cfg.CONF.set_override('local_ip', '10.10.10.10', group='OFS')
        cfg.CONF.set_override('tunnel_types', [p_const.TYPE_GRE,
                              p_const.TYPE_VXLAN], group='AGENT')
        cfgmap = self.mod_agent.create_agent_config_map(cfg.CONF)
        self.assertEqual(cfgmap['tunnel_types'],
                         [p_const.TYPE_GRE, p_const.TYPE_VXLAN])


class TestOFSNeutronAgentOVSBridge(OFSAgentTestCase):

    def setUp(self):
        super(TestOFSNeutronAgentOVSBridge, self).setUp()
        self.br_name = 'bridge1'
        self.root_helper = 'sudo'
        self.ovs = self.mod_agent.OVSBridge(self.br_name, self.root_helper)

    def test_set_controller(self):
        controller_names = ['tcp:127.0.0.1:6633', 'tcp:172.17.16.10:5555']
        with mock.patch.object(self.ovs, 'run_vsctl',
                               return_value=self.br_name) as mock_vsctl:
            self.ovs.set_controller(controller_names)
        mock_vsctl.assert_called_with(['--', 'set-controller', self.br_name,
                                      'tcp:127.0.0.1:6633',
                                      'tcp:172.17.16.10:5555'],
                                      check_error=True)

    def test_del_controller(self):
        with mock.patch.object(self.ovs, 'run_vsctl',
                               return_value=self.br_name) as mock_vsctl:
            self.ovs.del_controller()
        mock_vsctl.assert_called_with(['--', 'del-controller', self.br_name])

    def test_get_controller(self):
        with mock.patch.object(self.ovs, 'run_vsctl',
                               return_value=self.br_name) as mock_vsctl:
            mock_vsctl.return_value = 'tcp:127.0.0.1:6633\n' + \
                                      'tcp:172.17.16.10:5555'
            names = self.ovs.get_controller()
        self.assertEqual(names,
                         ['tcp:127.0.0.1:6633', 'tcp:172.17.16.10:5555'])
        mock_vsctl.assert_called_with(['--', 'get-controller', self.br_name])

    def test_set_protocols(self):
        protocols = 'OpenFlow13'
        with mock.patch.object(self.ovs, 'run_vsctl',
                               return_value=self.br_name) as mock_vsctl:
            self.ovs.set_protocols()
        mock_vsctl.assert_called_with(['--', 'set', 'bridge', self.br_name,
                                       "protocols=%s" % protocols],
                                      check_error=True)

    def test_find_datapath_id(self):
        with mock.patch.object(self.ovs, 'get_datapath_id',
                               return_value='12345') as mock_get_datapath_id:
            self.ovs.find_datapath_id()
        self.assertEqual(self.ovs.datapath_id, '12345')

    def _fake_get_datapath(self, app, datapath_id):
        if self.ovs.retry_count >= 2:
            datapath = mock.Mock()
            datapath.ofproto_parser = mock.Mock()
            return datapath
        self.ovs.retry_count += 1
        return None

    def test_get_datapath_normal(self):
        self.ovs.retry_count = 0
        with mock.patch.object(
            self.mod_agent.ryu_api, 'get_datapath',
            new=self._fake_get_datapath
        ) as mock_get_datapath:
            self.ovs.datapath_id = '0x64'
            self.ovs.get_datapath(retry_max=4)
        self.assertEqual(self.ovs.retry_count, 2)

    def test_get_datapath_retry_out_by_default_time(self):
        cfg.CONF.set_override('get_datapath_retry_times', 3, group='AGENT')
        with mock.patch('sys.exit', side_effect=Exception):
            with mock.patch.object(self.mod_agent.ryu_api, 'get_datapath',
                                   return_value=None) as mock_get_datapath:
                with testtools.ExpectedException(Exception):
                    self.ovs.datapath_id = '0x64'
                    self.ovs.get_datapath(retry_max=3)
        self.assertEqual(mock_get_datapath.call_count, 3)

    def test_get_datapath_retry_out_by_specified_time(self):
        with mock.patch('sys.exit', side_effect=Exception):
            with mock.patch.object(self.mod_agent.ryu_api, 'get_datapath',
                                   return_value=None) as mock_get_datapath:
                with testtools.ExpectedException(Exception):
                    self.ovs.datapath_id = '0x64'
                    self.ovs.get_datapath(retry_max=2)
        self.assertEqual(mock_get_datapath.call_count, 2)

    def test_setup_ofp_default_par(self):
        with contextlib.nested(
            mock.patch.object(self.ovs, 'set_protocols'),
            mock.patch.object(self.ovs, 'set_controller'),
            mock.patch.object(self.ovs, 'find_datapath_id'),
            mock.patch.object(self.ovs, 'get_datapath'),
        ) as (mock_set_protocols, mock_set_controller,
              mock_find_datapath_id, mock_get_datapath):
            self.ovs.setup_ofp()
        mock_set_protocols.assert_called_with('OpenFlow13')
        mock_set_controller.assert_called_with(['tcp:127.0.0.1:6633'])
        mock_get_datapath.assert_called_with(
            cfg.CONF.AGENT.get_datapath_retry_times)
        self.assertEqual(mock_find_datapath_id.call_count, 1)

    def test_setup_ofp_specify_par(self):
        controller_names = ['tcp:192.168.10.10:1234', 'tcp:172.17.16.20:5555']
        with contextlib.nested(
            mock.patch.object(self.ovs, 'set_protocols'),
            mock.patch.object(self.ovs, 'set_controller'),
            mock.patch.object(self.ovs, 'find_datapath_id'),
            mock.patch.object(self.ovs, 'get_datapath'),
        ) as (mock_set_protocols, mock_set_controller,
              mock_find_datapath_id, mock_get_datapath):
            self.ovs.setup_ofp(controller_names=controller_names,
                               protocols='OpenFlow133',
                               retry_max=11)
        mock_set_protocols.assert_called_with('OpenFlow133')
        mock_set_controller.assert_called_with(controller_names)
        mock_get_datapath.assert_called_with(11)
        self.assertEqual(mock_find_datapath_id.call_count, 1)

    def test_setup_ofp_with_except(self):
        with contextlib.nested(
            mock.patch('sys.exit', side_effect=Exception),
            mock.patch.object(self.ovs, 'set_protocols',
                              side_effect=Exception),
            mock.patch.object(self.ovs, 'set_controller'),
            mock.patch.object(self.ovs, 'find_datapath_id'),
            mock.patch.object(self.ovs, 'get_datapath'),
        ) as (mock_exit, mock_set_protocols, mock_set_controller,
              mock_find_datapath_id, mock_get_datapath):
            with testtools.ExpectedException(Exception):
                self.ovs.setup_ofp()


class TestOFSNeutronAgent(OFSAgentTestCase):

    def setUp(self):
        super(TestOFSNeutronAgent, self).setUp()
        self.addCleanup(cfg.CONF.reset)
        self.addCleanup(mock.patch.stopall)
        notifier_p = mock.patch(NOTIFIER)
        notifier_cls = notifier_p.start()
        self.notifier = mock.Mock()
        notifier_cls.return_value = self.notifier
        # Avoid rpc initialization for unit tests
        cfg.CONF.set_override('rpc_backend',
                              'neutron.openstack.common.rpc.impl_fake')
        kwargs = self.mod_agent.create_agent_config_map(cfg.CONF)

        class MockFixedIntervalLoopingCall(object):
            def __init__(self, f):
                self.f = f

            def start(self, interval=0):
                self.f()

        with contextlib.nested(
            mock.patch.object(self.mod_agent.OFSNeutronAgent,
                              'setup_integration_br',
                              return_value=mock.Mock()),
            mock.patch.object(self.mod_agent.OFSNeutronAgent,
                              'setup_ancillary_bridges',
                              return_value=[]),
            mock.patch.object(self.mod_agent.OVSBridge,
                              'get_local_port_mac',
                              return_value='00:00:00:00:00:01'),
            mock.patch('neutron.agent.linux.utils.get_interface_mac',
                       return_value='00:00:00:00:00:01'),
            mock.patch('neutron.openstack.common.loopingcall.'
                       'FixedIntervalLoopingCall',
                       new=MockFixedIntervalLoopingCall)):
            self.agent = self.mod_agent.OFSNeutronAgent(**kwargs)
            self.agent.tun_br = mock.Mock()
            self.datapath = mock.Mock()
            self.ofparser = mock.Mock()
            self.datapath.ofparser = self.ofparser
            self.ofparser.OFPMatch = mock.Mock()
            self.ofparser.OFPMatch.return_value = mock.Mock()
            self.ofparser.OFPFlowMod = mock.Mock()
            self.ofparser.OFPFlowMod.return_value = mock.Mock()
            self.agent.int_br.ofparser = self.ofparser

        self.agent.sg_agent = mock.Mock()

    def _mock_port_bound(self, ofport=None):
        port = mock.Mock()
        port.ofport = ofport
        net_uuid = 'my-net-uuid'
        with mock.patch.object(self.mod_agent.OVSBridge,
                               'set_db_attribute',
                               return_value=True):
            with mock.patch.object(self.agent,
                                   'ryu_send_msg') as ryu_send_msg_func:
                self.agent.port_bound(port, net_uuid, 'local', None, None)
        self.assertEqual(ryu_send_msg_func.called, ofport != -1)

    def test_port_bound_deletes_flows_for_valid_ofport(self):
        self._mock_port_bound(ofport=1)

    def test_port_bound_ignores_flows_for_invalid_ofport(self):
        self._mock_port_bound(ofport=-1)

    def test_port_dead(self):
        with mock.patch.object(self.mod_agent.OVSBridge,
                               'set_db_attribute',
                               return_value=True):
            with mock.patch.object(self.agent,
                                   'ryu_send_msg') as ryu_send_msg_func:
                port = mock.Mock()
                port.ofport = 2
                self.agent.port_dead(port)
        self.assertTrue(ryu_send_msg_func.called)

    def mock_update_ports(self, vif_port_set=None, registered_ports=None):
        with mock.patch.object(self.agent.int_br, 'get_vif_port_set',
                               return_value=vif_port_set):
            return self.agent.update_ports(registered_ports)

    def test_update_ports_returns_none_for_unchanged_ports(self):
        self.assertIsNone(self.mock_update_ports())

    def test_update_ports_returns_port_changes(self):
        vif_port_set = set([1, 3])
        registered_ports = set([1, 2])
        expected = dict(current=vif_port_set, added=set([3]), removed=set([2]))
        actual = self.mock_update_ports(vif_port_set, registered_ports)
        self.assertEqual(expected, actual)

    def test_treat_devices_added_returns_true_for_missing_device(self):
        with mock.patch.object(self.agent.plugin_rpc, 'get_device_details',
                               side_effect=Exception()):
            self.assertTrue(self.agent.treat_devices_added([{}]))

    def _mock_treat_devices_added(self, details, port, func_name):
        """Mock treat devices added.

        :param details: the details to return for the device
        :param port: the port that get_vif_port_by_id should return
        :param func_name: the function that should be called
        :returns: whether the named function was called
        """
        with contextlib.nested(
            mock.patch.object(self.agent.plugin_rpc, 'get_device_details',
                              return_value=details),
            mock.patch.object(self.agent.int_br, 'get_vif_port_by_id',
                              return_value=port),
            mock.patch.object(self.agent.plugin_rpc, 'update_device_up'),
            mock.patch.object(self.agent, func_name)
        ) as (get_dev_fn, get_vif_func, upd_dev_up, func):
            self.assertFalse(self.agent.treat_devices_added([{}]))
        return func.called

    def test_treat_devices_added_ignores_invalid_ofport(self):
        port = mock.Mock()
        port.ofport = -1
        self.assertFalse(self._mock_treat_devices_added(mock.MagicMock(), port,
                                                        'port_dead'))

    def test_treat_devices_added_marks_unknown_port_as_dead(self):
        port = mock.Mock()
        port.ofport = 1
        self.assertTrue(self._mock_treat_devices_added(mock.MagicMock(), port,
                                                       'port_dead'))

    def test_treat_devices_added_updates_known_port(self):
        details = mock.MagicMock()
        details.__contains__.side_effect = lambda x: True
        self.assertTrue(self._mock_treat_devices_added(details,
                                                       mock.Mock(),
                                                       'treat_vif_port'))

    def test_treat_devices_removed_returns_true_for_missing_device(self):
        with mock.patch.object(self.agent.plugin_rpc, 'update_device_down',
                               side_effect=Exception()):
            self.assertTrue(self.agent.treat_devices_removed([{}]))

    def _mock_treat_devices_removed(self, port_exists):
        details = dict(exists=port_exists)
        with mock.patch.object(self.agent.plugin_rpc, 'update_device_down',
                               return_value=details):
            with mock.patch.object(self.agent, 'port_unbound') as port_unbound:
                self.assertFalse(self.agent.treat_devices_removed([{}]))
        self.assertTrue(port_unbound.called)

    def test_treat_devices_removed_unbinds_port(self):
        self._mock_treat_devices_removed(True)

    def test_treat_devices_removed_ignores_missing_port(self):
        self._mock_treat_devices_removed(False)

    def test_process_network_ports(self):
        reply = {'current': set(['tap0']),
                 'removed': set(['eth0']),
                 'added': set(['eth1'])}
        with mock.patch.object(self.agent, 'treat_devices_added',
                               return_value=False) as device_added:
            with mock.patch.object(self.agent, 'treat_devices_removed',
                                   return_value=False) as device_removed:
                self.assertFalse(self.agent.process_network_ports(reply))
                self.assertTrue(device_added.called)
                self.assertTrue(device_removed.called)

    def test_report_state(self):
        with mock.patch.object(self.agent.state_rpc,
                               "report_state") as report_st:
            self.agent.int_br_device_count = 5
            self.agent._report_state()
            report_st.assert_called_with(self.agent.context,
                                         self.agent.agent_state)
            self.assertNotIn("start_flag", self.agent.agent_state)
            self.assertEqual(
                self.agent.agent_state["configurations"]["devices"],
                self.agent.int_br_device_count
            )

    def test_network_delete(self):
        with contextlib.nested(
            mock.patch.object(self.agent, "reclaim_local_vlan"),
            mock.patch.object(self.agent.tun_br, "cleanup_tunnel_port")
        ) as (recl_fn, clean_tun_fn):
            self.agent.network_delete("unused_context",
                                      network_id="123")
            self.assertFalse(recl_fn.called)
            self.agent.local_vlan_map["123"] = "LVM object"
            self.agent.network_delete("unused_context",
                                      network_id="123")
            self.assertFalse(clean_tun_fn.called)
            recl_fn.assert_called_with("123")

    def test_port_update(self):
        with contextlib.nested(
            mock.patch.object(self.agent.int_br, "get_vif_port_by_id"),
            mock.patch.object(self.agent, "treat_vif_port"),
            mock.patch.object(self.agent.plugin_rpc, "update_device_up"),
            mock.patch.object(self.agent.plugin_rpc, "update_device_down")
        ) as (getvif_fn, treatvif_fn, updup_fn, upddown_fn):
            port = {"id": "123",
                    "network_id": "124",
                    "admin_state_up": False}
            getvif_fn.return_value = "vif_port_obj"
            self.agent.port_update("unused_context",
                                   port=port,
                                   network_type="vlan",
                                   segmentation_id="1",
                                   physical_network="physnet")
            treatvif_fn.assert_called_with("vif_port_obj", "123",
                                           "124", "vlan", "physnet",
                                           "1", False)
            upddown_fn.assert_called_with(self.agent.context,
                                          "123", self.agent.agent_id,
                                          cfg.CONF.host)

            port["admin_state_up"] = True
            self.agent.port_update("unused_context",
                                   port=port,
                                   network_type="vlan",
                                   segmentation_id="1",
                                   physical_network="physnet")
            updup_fn.assert_called_with(self.agent.context,
                                        "123", self.agent.agent_id,
                                        cfg.CONF.host)

    def test_port_update_plugin_rpc_failed(self):
        port = {'id': 1,
                'network_id': 1,
                'admin_state_up': True}
        with contextlib.nested(
            mock.patch.object(self.mod_agent.LOG, 'error'),
            mock.patch.object(self.agent.int_br, "get_vif_port_by_id"),
            mock.patch.object(self.agent.plugin_rpc, 'update_device_up'),
            mock.patch.object(self.agent, 'port_bound'),
            mock.patch.object(self.agent.plugin_rpc, 'update_device_down'),
            mock.patch.object(self.agent, 'port_dead')
        ) as (log, _, device_up, _, device_down, _):
            device_up.side_effect = rpc_common.Timeout
            self.agent.port_update(mock.Mock(), port=port)
            self.assertTrue(device_up.called)
            self.assertEqual(log.call_count, 1)

            log.reset_mock()
            port['admin_state_up'] = False
            device_down.side_effect = rpc_common.Timeout
            self.agent.port_update(mock.Mock(), port=port)
            self.assertTrue(device_down.called)
            self.assertEqual(log.call_count, 1)

    def test_setup_physical_bridges(self):
        with contextlib.nested(
            mock.patch.object(ip_lib, "device_exists"),
            mock.patch.object(sys, "exit"),
            mock.patch.object(utils, "execute"),
            mock.patch.object(self.mod_agent.OVSBridge, "add_port"),
            mock.patch.object(self.mod_agent.OVSBridge, "delete_port"),
            mock.patch.object(self.mod_agent.OVSBridge, "set_protocols"),
            mock.patch.object(self.mod_agent.OVSBridge, "set_controller"),
            mock.patch.object(self.mod_agent.OVSBridge, "get_datapath_id",
                              return_value='0xa'),
            mock.patch.object(self.agent.int_br, "add_port"),
            mock.patch.object(self.agent.int_br, "delete_port"),
            mock.patch.object(ip_lib.IPWrapper, "add_veth"),
            mock.patch.object(ip_lib.IpLinkCommand, "delete"),
            mock.patch.object(ip_lib.IpLinkCommand, "set_up"),
            mock.patch.object(ip_lib.IpLinkCommand, "set_mtu"),
            mock.patch.object(self.mod_agent.ryu_api, "get_datapath",
                              return_value=self.datapath)
        ) as (devex_fn, sysexit_fn, utilsexec_fn,
              ovs_addport_fn, ovs_delport_fn, ovs_set_protocols_fn,
              ovs_set_controller_fn, ovs_datapath_id_fn, br_addport_fn,
              br_delport_fn, addveth_fn, linkdel_fn, linkset_fn, linkmtu_fn,
              ryu_api_fn):
            devex_fn.return_value = True
            parent = mock.MagicMock()
            parent.attach_mock(utilsexec_fn, 'utils_execute')
            parent.attach_mock(linkdel_fn, 'link_delete')
            parent.attach_mock(addveth_fn, 'add_veth')
            addveth_fn.return_value = (ip_lib.IPDevice("int-br-eth1"),
                                       ip_lib.IPDevice("phy-br-eth1"))
            ovs_addport_fn.return_value = "25"
            br_addport_fn.return_value = "11"
            cfg.CONF.ofp_listen_host = '127.0.0.1'
            cfg.CONF.ofp_tcp_listen_port = 6633
            self.agent.setup_physical_bridges({"physnet1": "br-eth"})
            expected_calls = [mock.call.link_delete(),
                              mock.call.utils_execute(['/sbin/udevadm',
                                                       'settle',
                                                       '--timeout=10']),
                              mock.call.add_veth('int-br-eth',
                                                 'phy-br-eth')]
            parent.assert_has_calls(expected_calls, any_order=False)
            self.assertEqual(self.agent.int_ofports["physnet1"],
                             "11")
            self.assertEqual(self.agent.phys_ofports["physnet1"],
                             "25")

    def test_port_unbound(self):
        with mock.patch.object(self.agent, "reclaim_local_vlan") as reclvl_fn:
            self.agent.enable_tunneling = True
            lvm = mock.Mock()
            lvm.network_type = "gre"
            lvm.vif_ports = {"vif1": mock.Mock()}
            self.agent.local_vlan_map["netuid12345"] = lvm
            self.agent.port_unbound("vif1", "netuid12345")
            self.assertTrue(reclvl_fn.called)
            reclvl_fn.called = False

            lvm.vif_ports = {}
            self.agent.port_unbound("vif1", "netuid12345")
            self.assertEqual(reclvl_fn.call_count, 2)

            lvm.vif_ports = {"vif1": mock.Mock()}
            self.agent.port_unbound("vif3", "netuid12345")
            self.assertEqual(reclvl_fn.call_count, 2)

    def _check_ovs_vxlan_version(self, installed_usr_version,
                                 installed_klm_version, min_vers,
                                 expecting_ok):
        with mock.patch(
                'neutron.agent.linux.ovs_lib.get_installed_ovs_klm_version'
        ) as klm_cmd:
            with mock.patch(
                'neutron.agent.linux.ovs_lib.get_installed_ovs_usr_version'
            ) as usr_cmd:
                try:
                    klm_cmd.return_value = installed_klm_version
                    usr_cmd.return_value = installed_usr_version
                    self.agent.tunnel_types = 'vxlan'
                    self.mod_agent.check_ovs_version(min_vers,
                                                     root_helper='sudo')
                    version_ok = True
                except SystemExit as e:
                    self.assertEqual(e.code, 1)
                    version_ok = False
            self.assertEqual(version_ok, expecting_ok)

    def test_check_minimum_version(self):
        self._check_ovs_vxlan_version('1.10', '1.10',
                                      constants.MINIMUM_OFS_VXLAN_VERSION,
                                      expecting_ok=True)

    def test_check_future_version(self):
        self._check_ovs_vxlan_version('1.11', '1.11',
                                      constants.MINIMUM_OFS_VXLAN_VERSION,
                                      expecting_ok=True)

    def test_check_fail_version(self):
        self._check_ovs_vxlan_version('1.9', '1.9',
                                      constants.MINIMUM_OFS_VXLAN_VERSION,
                                      expecting_ok=False)

    def test_check_fail_no_version(self):
        self._check_ovs_vxlan_version(None, None,
                                      constants.MINIMUM_OFS_VXLAN_VERSION,
                                      expecting_ok=False)

    def test_check_fail_klm_version(self):
        self._check_ovs_vxlan_version('1.10', '1.9',
                                      constants.MINIMUM_OFS_VXLAN_VERSION,
                                      expecting_ok=False)

    def test_daemon_loop_uses_polling_manager(self):
        with mock.patch(
            'neutron.agent.linux.polling.get_polling_manager'
        ) as mock_get_pm:
            with mock.patch.object(self.agent, 'rpc_loop') as mock_loop:
                self.agent.daemon_loop()
        mock_get_pm.assert_called_with(True, 'sudo',
                                       constants.DEFAULT_OFSDBMON_RESPAWN)
        mock_loop.called_once()

    def test_setup_tunnel_port_error_negative(self):
        with contextlib.nested(
            mock.patch.object(self.agent.tun_br, 'add_tunnel_port',
                              return_value='-1'),
            mock.patch.object(self.mod_agent.LOG, 'error')
        ) as (add_tunnel_port_fn, log_error_fn):
            ofport = self.agent.setup_tunnel_port(
                'gre-1', 'remote_ip', p_const.TYPE_GRE)
            add_tunnel_port_fn.assert_called_once_with(
                'gre-1', 'remote_ip', self.agent.local_ip, p_const.TYPE_GRE,
                self.agent.vxlan_udp_port)
            log_error_fn.assert_called_once_with(
                _("Failed to set-up %(type)s tunnel port to %(ip)s"),
                {'type': p_const.TYPE_GRE, 'ip': 'remote_ip'})
            self.assertEqual(ofport, 0)

    def test_setup_tunnel_port_error_not_int(self):
        with contextlib.nested(
            mock.patch.object(self.agent.tun_br, 'add_tunnel_port',
                              return_value=None),
            mock.patch.object(self.mod_agent.LOG, 'exception'),
            mock.patch.object(self.mod_agent.LOG, 'error')
        ) as (add_tunnel_port_fn, log_exc_fn, log_error_fn):
            ofport = self.agent.setup_tunnel_port(
                'gre-1', 'remote_ip', p_const.TYPE_GRE)
            add_tunnel_port_fn.assert_called_once_with(
                'gre-1', 'remote_ip', self.agent.local_ip, p_const.TYPE_GRE,
                self.agent.vxlan_udp_port)
            log_exc_fn.assert_called_once_with(
                _("ofport should have a value that can be "
                  "interpreted as an integer"))
            log_error_fn.assert_called_once_with(
                _("Failed to set-up %(type)s tunnel port to %(ip)s"),
                {'type': p_const.TYPE_GRE, 'ip': 'remote_ip'})
            self.assertEqual(ofport, 0)


class AncillaryBridgesTest(OFSAgentTestCase):

    def setUp(self):
        super(AncillaryBridgesTest, self).setUp()
        self.addCleanup(cfg.CONF.reset)
        self.addCleanup(mock.patch.stopall)
        notifier_p = mock.patch(NOTIFIER)
        notifier_cls = notifier_p.start()
        self.notifier = mock.Mock()
        notifier_cls.return_value = self.notifier
        # Avoid rpc initialization for unit tests
        cfg.CONF.set_override('rpc_backend',
                              'neutron.openstack.common.rpc.impl_fake')
        cfg.CONF.set_override('report_interval', 0, 'AGENT')
        self.kwargs = self.mod_agent.create_agent_config_map(cfg.CONF)

    def _test_ancillary_bridges(self, bridges, ancillary):
        device_ids = ancillary[:]

        def pullup_side_effect(self, *args):
            result = device_ids.pop(0)
            return result

        with contextlib.nested(
            mock.patch.object(self.mod_agent.OFSNeutronAgent,
                              'setup_integration_br',
                              return_value=mock.Mock()),
            mock.patch('neutron.agent.linux.utils.get_interface_mac',
                       return_value='00:00:00:00:00:01'),
            mock.patch.object(self.mod_agent.OVSBridge,
                              'get_local_port_mac',
                              return_value='00:00:00:00:00:01'),
            mock.patch('neutron.agent.linux.ovs_lib.get_bridges',
                       return_value=bridges),
            mock.patch(
                'neutron.agent.linux.ovs_lib.get_bridge_external_bridge_id',
                side_effect=pullup_side_effect)):
            self.agent = self.mod_agent.OFSNeutronAgent(**self.kwargs)
            self.assertEqual(len(ancillary), len(self.agent.ancillary_brs))
            if ancillary:
                bridges = [br.br_name for br in self.agent.ancillary_brs]
                for br in ancillary:
                    self.assertIn(br, bridges)

    def test_ancillary_bridges_single(self):
        bridges = ['br-int', 'br-ex']
        self._test_ancillary_bridges(bridges, ['br-ex'])

    def test_ancillary_bridges_none(self):
        bridges = ['br-int']
        self._test_ancillary_bridges(bridges, [])

    def test_ancillary_bridges_multiple(self):
        bridges = ['br-int', 'br-ex1', 'br-ex2']
        self._test_ancillary_bridges(bridges, ['br-ex1', 'br-ex2'])