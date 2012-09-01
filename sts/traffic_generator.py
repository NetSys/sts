
from pox.lib.packet.ethernet import *
from pox.lib.packet.ipv4 import *
from pox.lib.packet.icmp import *

class TrafficGenerator (object):
  """
  Generate sensible randomly generated (openflow) events
  """

  def __init__(self, random=random.Random()):
    self.random = random

    self._packet_generators = {
      "icmp_ping" : self.icmp_ping
    }

  def generate(self, packet_type, software_switch):
    if packet_type not in self._packet_generators:
      raise AttributeError("Unknown event type %s" % str(packet_type))

    # Feed the packet to the software_switch
    # TODO(cs): just use access links for packet ins -- not packets from within the network
    in_port = self.random.choice(software_switch.ports.values())
    packet = self._packet_generators[packet_type](software_switch, in_port)
    return software_switch.process_packet(packet, in_port=in_port.port_no)

  # Generates an ICMP ping, and feeds it to the software_switch
  def icmp_ping(self, software_switch, in_port):
    # randomly choose an in_port.
    if len(software_switch.ports) == 0:
      raise RuntimeError("No Ports Registered on software_switch! %s" % str(software_switch))
    e = ethernet()
    # TODO(cs): need a better way to create random MAC addresses
    e.src = EthAddr(struct.pack("Q",self.random.randint(1,0xFF))[:6])
    e.dst = in_port.hw_addr
    e.type = ethernet.IP_TYPE
    ipp = ipv4()
    ipp.protocol = ipv4.ICMP_PROTOCOL
    ipp.srcip = IPAddr(self.random.randint(0,0xFFFFFFFF))
    ipp.dstip = IPAddr(self.random.randint(0,0xFFFFFFFF))
    ping = icmp()
    ping.type = TYPE_ECHO_REQUEST
    ping.payload = "PingPing" * 6
    ipp.payload = ping
    e.payload = ipp
    return e

