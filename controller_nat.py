#Ryu app for Network Address Translation (NAT)
#The network topology used for experimentation shown below. 
#The network is divided into two subnets. Hosts h1, h2 and h3 belong to one of the subnets
# and h4 to the other. 
#
# 	          sw1-------sw2	
#            / | \       |
#           /  |  \      |
#          /   |   \     |
#         h1   h2  h3   h4
#
# Switch sw2 behaves as a router and performs NAT as directed by the SDN controller
# enabling hosts h1, h2 and h3 to reach h4. 


from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3

from ryu.lib import hub
from ryu.lib.packet import ether_types
from ryu.lib.packet import packet
from ryu.lib.packet import ethernet
from ryu.lib.packet import arp
from ryu.lib.packet import ipv4
from ryu.lib.packet import icmp
from ryu.ofproto import ether

import time
import IPy

class NATServer(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(NATServer, self).__init__(*args, **kwargs)
        self.ip_inside = IPy.IP('5.5.0.0/24')		#subnet for host 1, 2 and 3 
        self.ip_outside = IPy.IP('5.5.1.0/24')		#subnet for host 4 which is present in external network
        self.arp_table = {}				#initializing table to store ip address mapped to their mac address
        self.mac_to_port = {}				#initializing mac address table 	
	self.ipadress_to_associated_icmp_id = { }	#initializing table to store src ip address mapped to its corresponding icmp id
        self.ipaddress_to_identifier = { }		#initializing table to store ip address mapped to a unique identifier
	self.identifier = 1				#identifier used for constructing and handling icmp echo and reply messages
	self.identifier_to_ipaddress ={ }		#initializing table for mapping identifier to its ip address, this is used by the
							#router to recover source IP address of the querying host and forward the packet
							#it received from the remote node towards which, the query host initiated a ping

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
	#Install table miss flow entry
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
            ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 0, match, actions)

    def add_flow(self, datapath, priority, match, actions, buffer_id=None):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        inst = [parser.OFPInstructionActions(
            ofproto.OFPIT_APPLY_ACTIONS, actions)]
        if buffer_id:
            mod = parser.OFPFlowMod(datapath=datapath,
                buffer_id=buffer_id, priority=priority,
                match=match, instructions=inst)
        else:
            mod = parser.OFPFlowMod(datapath=datapath,
                priority=priority, match=match, instructions=inst)
        datapath.send_msg(mod)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        if ev.msg.msg_len < ev.msg.total_len:
            self.logger.debug("DEBUG: packet truncated: only %s of %s bytes",
                              ev.msg.msg_len, ev.msg.total_len)
        msg = ev.msg
        datapath = msg.datapath
        dpid = datapath.id
        if dpid != 1:
            return
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
	#fetch the received port number from the packet_in message
        in_port = msg.match['in_port']
	#Analyze the received packet using Ryu packet library
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocols(ethernet.ethernet)[0]
        dst = eth.dst
        src = eth.src
        data = None
        self.mac_to_port.setdefault(dpid, {})
        self.logger.info("DEBUG: packet in %s %s %s %s", dpid, src, dst, in_port)
	#learn a mac address to avoid flooding in future 
        self.mac_to_port[dpid][src] = in_port
	# if the destination mac address is already learned,
	# decide which port to output the packet, otherwise FLOOD.
        if dst in self.mac_to_port[dpid]:
            out_port = self.mac_to_port[dpid][dst]
        else:
            out_port = ofproto.OFPP_FLOOD
        actions = [parser.OFPActionOutput(out_port)]
	# install a flow to avoid packet_in next time.
	if out_port != ofproto.OFPP_FLOOD:
            match = parser.OFPMatch(in_port=in_port, eth_dst=dst)
            # verify if we have a valid buffer_id, if yes avoid to send both
            # flow_mod & packet_out
            if msg.buffer_id != ofproto.OFP_NO_BUFFER:
                self.add_flow(datapath, 1, match, actions, msg.buffer_id)
                return
            else:
                self.add_flow(datapath, 1, match, actions)
        data = None
        if msg.buffer_id == ofproto.OFP_NO_BUFFER:
            data = msg.data
	# construct packet_out message and send it.
        out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                  in_port=in_port, actions=actions, data=data)
        datapath.send_msg(out)
	

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    #packet in handler for packet coming from the router
    def _packet_in_handler_router(self, ev):
	msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        dpid = datapath.id
	#Check if packets came from the router
        if dpid != 2:	
            return
	#self.logger.info("Debug: inside nat packet handler\n")
        port = msg.match['in_port']
	self.logger.info("DEBUG: inside nat packet handler, port is %s\n", port)
        pkt = packet.Packet(msg.data)
        eth_req = pkt.get_protocols(ethernet.ethernet)[0]
        arp_req = pkt.get_protocol(arp.arp)
        icmp_req = pkt.get_protocol(icmp.icmp)
	ipv4_req = pkt.get_protocol(ipv4.ipv4)

	if arp_req:
            if (arp_req.opcode == arp.ARP_REQUEST and arp_req.dst_ip in self.ip_inside): 
                return
	    else:
                self.arp_handler(datapath, arp_req)
        if icmp_req:
	    self.icmp_handler(datapath, pkt)
        return

    def arp_handler(self, dp, arp_req):
	if arp_req.src_ip not in self.arp_table:
            self.arp_table[arp_req.src_ip] = arp_req.src_mac
        if arp_req.opcode == arp.ARP_REQUEST:         
	    #If the router received ARP request from a host for a IP in outside network, 
	    #it will pretend to be that host and give it's own MAC address (ARP Proxy) 
	    # pretend to be that host and give it's own MAC address (ARP Proxy)     						     
            if arp_req.src_ip in self.ip_inside: 
	        arp_rep = packet.Packet()
		#router maintains two ports, one for each network. '1' is for the network 
		#in which hosts h1, h2 and h3 are and '2' is for the network in which host 
		#h4 is. For replying to the ARP Request received from the either of 
		#h1, h2 or h3, router will use the MAC address corresponding to the port and '1'
                eth_rep = ethernet.ethernet(ethertype=ether_types.ETH_TYPE_ARP, 
					    dst=arp_req.src_mac, 
					    src=dp.ports[1].hw_addr)

                arp_rep_pkt = arp.arp(opcode=arp.ARP_REPLY, 
				      src_mac=dp.ports[1].hw_addr, 
				      src_ip=arp_req.dst_ip, 
				      dst_mac=arp_req.src_mac, 
				      dst_ip=arp_req.src_ip)		

                arp_rep.add_protocol(eth_rep)
                arp_rep.add_protocol(arp_rep_pkt)
                self.send_packet(dp, 1, arp_rep)

	    if arp_req.src_ip in self.ip_outside:
                arp_reply = packet.Packet()
		#router will src mac address corresponding to port '2' as ARP request is 
		#received from h4
                reply_eth_pkt = ethernet.ethernet(ethertype=ether.ETH_TYPE_ARP, 
						  dst=arp_req.src_mac, 
						  src=dp.ports[2].hw_addr)

                reply_arp_pkt = arp.arp(opcode=arp.ARP_REPLY, 
					src_mac=dp.ports[2].hw_addr, 
					src_ip=arp_req.dst_ip, 
					dst_mac=arp_req.src_mac, 
					dst_ip=arp_req.src_ip)

                arp_reply.add_protocol(reply_eth_pkt)
                arp_reply.add_protocol(reply_arp_pkt)
                self.send_packet(dp, 2, arp_reply)
                return

    def icmp_handler(self,datapath, pkt):
	eth_pkt = pkt.get_protocols(ethernet.ethernet)[0]
        ipv4_pkt = pkt.get_protocol(ipv4.ipv4)
        icmp_pkt = pkt.get_protocol(icmp.icmp)
	
	#if an ICMP Request packet was received by the router
	if icmp_pkt.type == icmp.ICMP_ECHO_REQUEST:
            if ipv4_pkt.src not in self.ipaddress_to_identifier: 
                self.ipaddress_to_identifier[ipv4_pkt.src] = self.identifier
                self.identifier_to_ipaddress[self.identifier] = ipv4_pkt.src
                self.identifier += 1	#to maintain a unique identifier for each ip address
		self.logger.info("DEBUG: self.id_number after increment is %s\n", self.identifier)

            self.ipadress_to_associated_icmp_id[ipv4_pkt.src] =icmp_pkt.data.id
	    self.logger.info("DEBUG: icmp_pkt.data.id inside echo request is %s\n", icmp_pkt.data.id)

	    #In order to forward the packet to correct destination, the router should its MAC address
	    if ipv4_pkt.dst in self.arp_table: #checking if IP address is already present in the ARP table
                dst = self.arp_table[ipv4_pkt.dst]
	    else:			
		#IP address not found in ARP table, construct an ARP Request packet and send to the destination
		arp_request = packet.Packet()
                eth_req = ethernet.ethernet(ethertype=ether_types.ETH_TYPE_ARP, dst='ff:ff:ff:ff:ff:ff', src=datapath.ports[1].hw_addr)
                arp_request_pkt = arp.arp(opcode=arp.ARP_REQUEST,
                dst_ip=ipv4_pkt.dst,          
                dst_mac='00:00:00:00:00:00',  
		src_ip = '5.5.1.0',
                src_mac=datapath.ports[2].hw_addr)
                arp_request.add_protocol(eth_req)
                arp_request.add_protocol(arp_request_pkt)
                self.send_packet(datapath, 2, arp_request)  
		try:
		    #the destination should have responded with an ARP REPLY and it's entry should've been stored in the ARP table
                    dst_mac = self.arp_table[ipv4_pkt.dst]
                except KeyError:
                    return

            #MAC address of destination is now known, replace the source ip address by router's public IP address (outside IP address)
	    #and id of the icmp_echo_request ,send it tho the external network
            tx_eth_pkt = ethernet.ethernet(ethertype = ether.ETH_TYPE_IP, dst = self.arp_table[ipv4_pkt.dst], src = datapath.ports[2].hw_addr)

            tx_icmp_echo_pkt = icmp.echo(id_ = self.ipaddress_to_identifier[ipv4_pkt.src], seq = icmp_pkt.data.seq, data = icmp_pkt.data.data)

            tx_icmp_pkt = icmp.icmp(type_ = icmp_pkt.type, code = icmp_pkt.code, csum = 0, data = tx_icmp_echo_pkt)
            
	    ipv4_pkt.src = '5.5.1.0'
            icmp_tx_pkt = packet.Packet()
            icmp_tx_pkt.add_protocol(tx_eth_pkt)
            icmp_tx_pkt.add_protocol(ipv4_pkt)
            icmp_tx_pkt.add_protocol(tx_icmp_pkt)
            self.send_packet(datapath,2,icmp_tx_pkt)
            return

        #When router receives an ICMP Reply from the destination node, do a look up ip in the 'identifier to ip address' tabele 
	#for the received indentifier to determine the destination ip address 
        #While forwarding the ICMP Reply message, router will put src IP as the IP of the remote node (from where the ICMP REPLY came)
	#to let the querying host feel that the response came from the remote node.  
        if icmp_pkt.type == icmp.ICMP_ECHO_REPLY:
	    self.logger.info("DEBUG: icmp_pkt.data.id inside echo reply is %s\n", icmp_pkt.data.id)
            orignal_ip = self.identifier_to_ipaddress[icmp_pkt.data.id]
	    self.logger.info("DEBUG: original_ip is %s\n", orignal_ip)
            orignal_id = self.ipadress_to_associated_icmp_id[orignal_ip]
	    self.logger.info("DEBUG: orignal_id inside echo reply %s\n", orignal_id)

	    #Construct ICMP packet and forward to the querying host
            tx_eth_pkt = ethernet.ethernet(ethertype = ether.ETH_TYPE_IP, dst = self.arp_table[orignal_ip], src = datapath.ports[1].hw_addr)

            tx_icmp_reply_pkt = icmp.echo(id_ = orignal_id, seq = icmp_pkt.data.seq, data = icmp_pkt.data.data)

            tx_icmp_pkt = icmp.icmp(type_ = icmp_pkt.type, code = icmp_pkt.code, csum = 0, data = tx_icmp_reply_pkt)
            
	    ipv4_pkt.dst = orignal_ip
            icmp_tx_pkt = packet.Packet()
            icmp_tx_pkt.add_protocol(tx_eth_pkt)
            icmp_tx_pkt.add_protocol(ipv4_pkt)
            icmp_tx_pkt.add_protocol(tx_icmp_pkt)
            self.send_packet(datapath,1, icmp_tx_pkt)
            return

    def send_packet(self, datapath, port, pkt): #for constructing message and sending it out
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        pkt.serialize()
        data = pkt.data
        actions = [parser.OFPActionOutput(port=port)]
        out = parser.OFPPacketOut(datapath=datapath,
            buffer_id=ofproto.OFP_NO_BUFFER,
            in_port=ofproto.OFPP_CONTROLLER,
            actions=actions, data=data)
        datapath.send_msg(out)
 

