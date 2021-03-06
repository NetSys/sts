"""
This module mocks out openflow switches, links, and hosts. These are all the
'entities' that exist within our simulated environment.
"""

from pox.openflow.software_switch import DpPacketOut, OFConnection
from pox.openflow.nx_software_switch import NXSoftwareSwitch
from pox.openflow.flow_table import FlowTableModification
from pox.openflow.libopenflow_01 import *
from pox.lib.revent import EventMixin
import pox.lib.packet.ethernet as eth
from sts.util.procutils import popen_filtered, kill_procs
from sts.util.console import msg
from itertools import count
from pox.lib.addresses import EthAddr, IPAddr

import logging
import os
import socket
import subprocess
import fcntl
import struct
import re
import pickle
import sys

from pox.lib.addresses import EthAddr
from os import geteuid
from exceptions import EnvironmentError
from platform import system

class DeferredOFConnection(OFConnection):
  def __init__(self, io_worker, cid, dpid, god_scheduler):
    super(DeferredOFConnection, self).__init__(io_worker)
    self.cid = cid
    self.dpid = dpid
    self.god_scheduler = god_scheduler
    # Don't feed messages to the switch directly
    self.on_message_received = self.insert_pending_receipt
    self.true_on_message_handler = None

  def get_controller_id(self):
    return self.cid

  def insert_pending_receipt(self, _, ofp_msg):
    ''' Rather than pass directly on to the switch, feed into the god scheduler'''
    self.god_scheduler.insert_pending_receipt(self.dpid, self.cid, ofp_msg, self)

  def set_message_handler(self, handler):
    ''' Take the switch's handler, and store it for later use '''
    self.true_on_message_handler = handler

  def allow_message_receipt(self, ofp_message):
    ''' Allow the message to actually go through to the switch '''
    self.true_on_message_handler(self, ofp_message)

  def send(self, ofp_message):
    ''' Interpose on switch sends as well '''
    self.god_scheduler.insert_pending_send(self.dpid, self.cid, ofp_message, self)

  def allow_message_send(self, ofp_message):
    ''' Allow message actually be sent to the controller '''
    super(DeferredOFConnection, self).send(ofp_message)

class FuzzSoftwareSwitch (NXSoftwareSwitch):
  """
  A mock switch implementation for testing purposes. Can simulate dropping dead.
  """
  _eventMixin_events = set([DpPacketOut])

  def __init__(self, dpid, name=None, ports=4, miss_send_len=128,
               n_buffers=100, n_tables=1, capabilities=None,
               can_connect_to_endhosts=True):
    NXSoftwareSwitch.__init__(self, dpid, name, ports, miss_send_len,
                              n_buffers, n_tables, capabilities)

    # Whether this is a core or edge switch
    self.can_connect_to_endhosts = can_connect_to_endhosts
    self.create_connection = None

    self.failed = False
    self.log = logging.getLogger("FuzzSoftwareSwitch(%d)" % dpid)

    if logging.getLogger().getEffectiveLevel() <= logging.DEBUG:
       def _print_entry_remove(table_mod):
         if table_mod.removed != []:
           self.log.debug("Table entry removed %s" % str(table_mod.removed))
       self.table.addListener(FlowTableModification, _print_entry_remove)

    def error_handler(e):
      self.log.exception(e)
      raise e

    # controller (ip, port) -> connection
    self.cid2connection = {}
    self.error_handler = error_handler
    self.controller_info = []

  def add_controller_info(self, info):
    self.controller_info.append(info)

  def _handle_ConnectionUp(self, event):
    self._setConnection(event.connection, event.ofp)

  def connect(self, create_connection, down_controller_ids=None):
    ''' - create_connection is a factory method for creating Connection objects
          which are connected to controllers. Takes a ControllerConfig object
          and a reference to a switch (self) as a paramter
    '''
    # Keep around the connection factory for fail/recovery later
    if down_controller_ids is None:
      down_controller_ids = set()
    self.create_connection = create_connection
    connected_to_at_least_one = False
    for info in self.controller_info:
      # Don't connect to down controllers
      if info.cid not in down_controller_ids:
        conn = create_connection(info, self)
        self.set_connection(conn)
        # cause errors to be raised
        conn.error_handler = self.error_handler
        # controller (ip, port) -> connection
        self.cid2connection[info.cid] = conn
        connected_to_at_least_one = True

    return connected_to_at_least_one

  def send(self, *args, **kwargs):
    if self.failed:
      self.log.warn("Currently down. Dropping send()")
    else:
      super(FuzzSoftwareSwitch, self).send(*args, **kwargs)

  def get_connection(self, cid):
    if cid not in self.cid2connection:
      raise ValueError("No such connection %s" % str(cid))
    return self.cid2connection[cid]

  def fail(self):
    # TODO(cs): depending on the type of failure, a real switch failure
    # might not lead to an immediate disconnect
    if self.failed:
      self.log.warn("Switch already failed")
      return
    self.failed = True

    for connection in self.connections:
      connection.close()
    self.connections = []

  def recover(self, down_controller_ids=None):
    if not self.failed:
      self.log.warn("Switch already up")
      return
    if self.create_connection is None:
      self.log.warn("Never connected in the first place")

    connected_to_at_least_one = self.connect(self.create_connection,
                                             down_controller_ids=down_controller_ids)
    if connected_to_at_least_one:
      self.failed = False
    return connected_to_at_least_one

  def serialize(self):
    # Skip over non-serializable data, e.g. sockets
    # TODO(cs): is self.log going to be a problem?
    serializable = FuzzSoftwareSwitch(self.dpid, self.parent_controller_name)
    # Can't serialize files
    serializable.log = None
    # TODO(cs): need a cleaner way to add in the NOM port representation
    if self.software_switch:
      serializable.ofp_phy_ports = self.software_switch.ports.values()
    return pickle.dumps(serializable, protocol=0)

class Link (object):
  """
  A network link between two switches

  Temporary stand in for Murphy's graph-library for the NOM.

  Note: Directed!
  """
  def __init__(self, start_software_switch, start_port, end_software_switch, end_port):
    if type(start_port) == int:
      assert(start_port in start_software_switch.ports)
      start_port = start_software_switch.ports[start_port]
    if type(end_port) == int:
      assert(end_port in start_software_switch.ports)
      end_port = end_software_switch.ports[end_port]
    assert_type("start_port", start_port, ofp_phy_port, none_ok=False)
    assert_type("end_port", end_port, ofp_phy_port, none_ok=False)
    self.start_software_switch = start_software_switch
    self.start_port = start_port
    self.end_software_switch = end_software_switch
    self.end_port = end_port

  def __eq__(self, other):
    if not type(other) == Link:
      return False
    return (self.start_software_switch == other.start_software_switch and
            self.start_port == other.start_port and
            self.end_software_switch == other.end_software_switch and
            self.end_port == other.end_port)

  def __ne__(self, other):
    # NOTE: __ne__ in python does *NOT* by default delegate to eq
    return not self.__eq__(other)


  def __hash__(self):
    return (self.start_software_switch.__hash__() +  self.start_port.__hash__() +
           self.end_software_switch.__hash__() +  self.end_port.__hash__())

  def __repr__(self):
    return "(%d:%d) -> (%d:%d)" % (self.start_software_switch.dpid, self.start_port.port_no,
                                   self.end_software_switch.dpid, self.end_port.port_no)

  def reversed_link(self):
    '''Create a Link that is in the opposite direction of this Link.'''
    return Link(self.end_software_switch, self.end_port,
                self.start_software_switch, self.start_port)

class AccessLink (object):
  '''
  Represents a bidirectional edge: host <-> ingress switch
  '''
  def __init__(self, host, interface, switch, switch_port):
    assert_type("interface", interface, HostInterface, none_ok=False)
    assert_type("switch_port", switch_port, ofp_phy_port, none_ok=False)
    self.host = host
    self.interface = interface
    self.switch = switch
    self.switch_port = switch_port

class HostInterface (object):
  ''' Represents a host's interface (e.g. eth0) '''
  def __init__(self, hw_addr, ip_or_ips=[], name=""):
    self.hw_addr = hw_addr
    if type(ip_or_ips) != list:
      ip_or_ips = [ip_or_ips]
    self.ips = ip_or_ips
    self.name = name

  @property
  def port_no(self):
    # Hack
    return self.hw_addr.toStr()

  def __eq__(self, other):
    if type(other) != HostInterface:
      return False
    if self.hw_addr.toInt() != other.hw_addr.toInt():
      return False
    other_ip_ints = map(lambda ip: ip.toUnsignedN(), other.ips)
    for ip in self.ips:
      if ip.toUnsignedN() not in other_ip_ints:
        return False
    if len(other.ips) != len(self.ips):
      return False
    if self.name != other.name:
      return False
    return True

  def __hash__(self):
    hash_code = self.hw_addr.toInt().__hash__()
    for ip in self.ips:
      hash_code += ip.toUnsignedN().__hash__()
    hash_code += self.name.__hash__()
    return hash_code

  def __str__(self, *args, **kwargs):
    return "HostInterface:" + self.name + ":" + str(self.hw_addr) + ":" + str(self.ips)

  def __repr__(self, *args, **kwargs):
    return self.__str__()

#                Host
#          /      |       \
#  interface   interface  interface
#    |            |           |
# access_link acccess_link access_link
#    |            |           |
# switch_port  switch_port  switch_port

class Host (EventMixin):
  '''
  A very simple Host entity.

  For more sophisticated hosts, we should spawn a separate VM!

  If multiple host VMs are too heavy-weight for a single machine, run the
  hosts on their own machines!
  '''
  _eventMixin_events = set([DpPacketOut])
  _hids = count(1)

  def __init__(self, interfaces, name=""):
    '''
    - interfaces A list of HostInterfaces
    '''
    self.interfaces = interfaces
    self.log = logging.getLogger(name)
    self.name = name
    self.hid = self._hids.next()

  def send(self, interface, packet):
    ''' Send a packet out a given interface '''
    self.log.info("sending packet on interface %s: %s" % (interface.name, str(packet)))
    self.raiseEvent(DpPacketOut(self, packet, interface))

  def receive(self, interface, packet):
    '''
    Process an incoming packet from a switch

    Called by PatchPanel
    '''
    self.log.info("received packet on interface %s: %s" % (interface.name, str(packet)))

  @property
  def dpid(self):
    # Hack
    return self.name

  def __str__(self):
    return self.name

  def __repr__(self):
    return "Host(%d)" % self.hid

class NamespaceHost(Host):
  '''
  A host that launches a process in a separate namespace process.

  '''
  ETH_P_ALL = 3                     # from linux/if_ether.h

  def __init__(self, ip_addr_str, create_io_worker, name="", cmd="xterm"):
    '''
    - ip_addr_str must be a string! not a IPAddr object
    - cmd: a string of the command to execute in the separate namespace
      The default is "xterm", which opens up a new terminal window.
    '''
    self.hid = self._hids.next()
    self.socket = None
    self.guest = None
    self.guest_eth_addr = None
    self.guest_device = None
    self._launch_namespace(cmd, ip_addr_str, create_io_worker)
    self.interfaces = [HostInterface(self.guest_eth_addr, IPAddr(ip_addr_str))]
    if name == "":
      name = "host:" + ip_addr_str
    self.name = name

  def _launch_namespace(self, cmd, ip_addr_str, create_io_worker):
    '''
    Set up and launch cmd in a new network namespace.

    Returns a tuple of the (socket, Popen object of unshared project in netns, EthAddr of guest device).

    This method uses functionality that requires CAP_NET_ADMIN capabilites. This
    means that the calling method should check that the python process was
    launched as admin/superuser.

    Parameters:
      - cmd: the string to launch, in a separate namespace
    '''

    if system() != 'Linux':
      raise EnvironmentError('network namespace functionality requires a Linux environment')

    uid = geteuid()
    if uid != 0:
      # user must have CAP_NET_ADMIN, which doesn't have to be su, but most often is
      raise EnvironmentError("superuser privileges required to launch network namespace")

    iface_index = self.hid

    host_device = "heth%d" % (iface_index)
    guest_device = "geth%d" % (iface_index)

    try:
      null = open(os.devnull, 'wb') # FIXME(sw): this file is never actually closed

      # Clean up previos network namespaces
      # (Delete the device if it already exists)
      for dev in (host_device, guest_device):
        if subprocess.call(['ip', 'link', 'show', dev], stdout=null, stderr=null) == 0:
          subprocess.check_call(['ip', 'link', 'del', dev])

      # create a veth pair and set the host end to be promiscuous
      subprocess.check_call(['ip','link','add','name',host_device,'type','veth','peer','name',guest_device])
      subprocess.check_call(['ip','link','set',host_device,'promisc','on'])
      # Our end of the veth pair
      subprocess.check_call(['ip','link','set',host_device,'up'])
    except subprocess.CalledProcessError:
      raise # TODO raise a more informative exception

    guest_eth_addr = self.get_eth_address_for_interface(guest_device)

    # make the host-side (STS-side) socket
    # do this before unshare/fork to make failure/cleanup easier
    # Make sure we aren't monkeypatched first:
    if hasattr(socket, "_old_socket"):
      raise RuntimeError("MonkeyPatched socket! Bailing")
    s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, self.ETH_P_ALL)
    # Make sure the buffers are big enough to fit at least one full ethernet
    # packet
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8192)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8192)
    s.bind((host_device, self.ETH_P_ALL))
    s.setblocking(0) # set non-blocking

    # all else should have succeeded, so now we fork and unshare for the guest
    # `ifconfig $ifname set ip $ifaddr netmask 255.255.255.0 up ; xterm`
    guest = subprocess.Popen(["unshare", "-n", "--", "/bin/bash"],
                             stdin=subprocess.PIPE)

    # push down the guest device into the netns
    try:
      subprocess.check_call(['ip', 'link', 'set', guest_device, 'netns', str(guest.pid)])
    except subprocess.CalledProcessError:
      # Failed to push down guest side of veth pair
      s.close()
      raise # TODO raise a more informative exception

    # Set the IP address of the virtual interface
    # TODO(cs): currently failing with the following error:
    #   set: Host name lookup failure
    #   ifconfig: `--help' gives usage information.
    # I think we may need to add an entry to /etc/hosts before invoking
    # ifconfig
    # For now, just force the user to configure it themselves in the xterm
    #guest.communicate("ifconfig %s set ip %s netmask 255.255.255.0 up" %
    #                  (guest_device,ip_addr_str))
    # Send the command
    guest.communicate(cmd)

    self.socket = s
    # Set up an io worker for our end of the socket
    self.io_worker = create_io_worker(self.socket)
    self.io_worker.set_receive_handler(self.send)
    self.guest = guest
    self.guest_eth_addr = guest_eth_addr
    self.guest_device = guest_device

  @staticmethod
  def get_eth_address_for_interface(ifname):
    '''Returns an EthAddr object from the interface specified by the argument.

    interface is a string, commonly eth0, wlan0, lo.'''
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    info = fcntl.ioctl(s.fileno(), 0x8927,  struct.pack('256s', ifname[:15]))
    return EthAddr(''.join(['%02x:' % ord(char) for char in info[18:24]])[:-1])

  def send(self, io_worker):
    message = io_worker.peek_receive_buf()
    # Create an ethernet packet
    # TODO(cs): this assumes that the raw socket returns exactly one ethernet
    # packet. Since ethernet frames do not include length information, the
    # only way to correctly handle partial packets would be to get access to
    # framing information. Should probably look at what Mininet does.
    packet = eth.ethernet(raw=message)
    if not packet.parsed:
      return
    io_worker.consume_receive_buf(packet.hdr_len + packet.payload_len)
    super(NamespaceHost, self).send(packet)

  def receive(self, interface, packet):
    '''
    Process an incoming packet from a switch

    Called by PatchPanel
    '''
    self.log.info("received packet on interface %s: %s. Passing to netns" %
                  (interface.name, str(packet)))
    self.io_worker.send(packet.pack())


class Controller(object):
  '''Encapsulates the state of a running controller.'''

  _active_processes = set() # set of processes that are currently running. These are all killed upon signal reception

  @staticmethod
  def kill_active_procs():
    '''Kill the active processes. Used by the simulator module to shut down the
    controllers because python can only have a single method to handle SIG* stuff.'''
    kill_procs(Controller._active_processes)

  def _register_proc(self, proc):
    '''Register a Popen instance that a controller is running in for the cleanup
    that happens when the simulator receives a signal. This method is idempotent.'''
    self._active_processes.add(proc)

  def _unregister_proc(self, proc):
    '''Remove a process from the set of this to be killed when a signal is
    received. This is for use when the Controller process is stopped. This
    method is idempotent.'''
    self._active_processes.discard(proc)

  def __del__(self):
    if hasattr(self, 'process') and self.process != None: # if it fails in __init__, process may not have been assigned
      if self.process.poll():
        self._unregister_proc(self.process) # don't let this happen for shutdown
      else:
        self.kill() # make sure it is killed if this was started errantly

  def __init__(self, controller_config, sync_connection_manager,
               snapshot_service):
    '''idx is the unique index for the controller used mostly for logging purposes.'''
    self.config = controller_config
    self.alive = False
    self.process = None
    self.sync_connection_manager = sync_connection_manager
    self.sync_connection = None
    self.snapshot_service = snapshot_service
    self.log = logging.getLogger("Controller")

  @property
  def pid(self):
    '''Return the PID of the Popen instance the controller was started with.'''
    return self.process.pid if self.process else None

  @property
  def label(self):
    '''Return the label of this controller. See ControllerConfig for more details.'''
    return self.config.label

  @property
  def cid(self):
    '''Return the id of this controller. See ControllerConfig for more details.'''
    return self.config.label

  def kill(self):
    '''Kill the process the controller is running in.'''
    msg.event("Killing controller %s" % (str(self.cid)))
    if self.sync_connection:
      self.sync_connection.close()

    kill_procs([self.process])
    self._unregister_proc(self.process)
    self.alive = False
    self.process = None

  def start(self):
    '''Start a new controller process based on the config's cmdline
    attribute. Registers the Popen member variable for deletion upon a SIG*
    received in the simulator process.'''
    msg.event("Starting controller %s" % (str(self.cid)))
    env = None

    if self.config.sync:
      # if a sync connection has been configured in the controller conf
      # launch the controller with environment variable 'sts_sync' set
      # to the appropriate listening port. This is quite a hack.
      env = os.environ.copy()
      port_match = re.search(r':(\d+)$', self.config.sync)
      if port_match is None:
        raise ValueError("sync: cannot find port in %s" % self.config.sync)
      port = port_match.group(1)
      env['sts_sync'] = "ptcp:0.0.0.0:%d" % (int(port),)

      if self.config.name == "pox":
        src_dir = os.path.join(os.path.dirname(__file__), "..")
        pox_ext_dir = os.path.join(self.config.cwd, "ext")
        if os.path.exists(pox_ext_dir):
          for f in ("sts/util/io_master.py", "sts/syncproto/base.py",
                    "sts/syncproto/pox_syncer.py", "sts/__init__.py",
                    "sts/util/socket_mux/__init__.py",
                    "sts/util/socket_mux/pox_monkeypatcher.py",
                    "sts/util/socket_mux/base.py",
                    "sts/util/socket_mux/server_socket_multiplexer.py"):
            src_path = os.path.join(src_dir, f)
            if not os.path.exists(src_path):
              raise ValueError("Integrity violation: sts sync source path %s (abs: %s) does not exist" %
                  (src_path, os.path.abspath(src_path)))
            dst_path = os.path.join(pox_ext_dir, f)
            dst_dir = os.path.dirname(dst_path)
            init_py = os.path.join(dst_dir, "__init__.py")
            if not os.path.exists(dst_dir):
              os.makedirs(dst_dir)

            if not os.path.exists(init_py):
              open(init_py, "a").close()

            if os.path.islink(dst_path):
              # remove symlink and recreate
              os.remove(dst_path)

            if not os.path.exists(dst_path):
              rel_link = os.path.abspath(src_path)
              self.log.debug("creating symlink %s -> %s", rel_link, dst_path)
              os.symlink(rel_link, dst_path)
        else:
          self.log.warn("Could not find pox ext dir in %s. Cannot check/link in sync module" % pox_ext_dir)

    self.log.info("Launching controller %s: %s" % (self.label, " ".join(self.config.expanded_cmdline)))
    self.process = popen_filtered("[%s]"%self.label, self.config.expanded_cmdline, self.config.cwd, env=env)
    self._register_proc(self.process)

    if self.config.sync:
      self.sync_connection = self.sync_connection_manager.connect(self, self.config.sync)

    self.alive = True

  def restart(self):
    self.kill()
    self.start()

  def check_process_status(self):
    if not self.alive:
      return (True, "OK")
    else:
      if not self.process:
        return (False, "Controller %s: Alive, but no controller process found" % self.config.name)
      rc = self.process.poll()
      if rc is not None:
        return (False, "Controller %s: Alive, but controller process terminated with return code %d" % ( self.config.name, rc))
      return (True, "OK")

  def send_policy_request(self, controller, api_call):
    pass

