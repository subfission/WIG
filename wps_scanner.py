#!/usr/bin/env python
# -*- coding: iso-8859-15 -*-
#
# BSD 3-Clause License
#
# Copyright (c) 2016, Andr�s Blanco (6e726d@gmail.com)
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# * Redistributions of source code must retain the above copyright notice, this
#   list of conditions and the following disclaimer.
#
# * Redistributions in binary form must reproduce the above copyright notice,
#   this list of conditions and the following disclaimer in the documentation
#   and/or other materials provided with the distribution.
#
# * Neither the name of WIG nor the names of its
#   contributors may be used to endorse or promote products derived from
#   this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.


import sys
import time
import string
import signal
import struct

from multiprocessing import Process

import pcapy

from impacket import ImpactDecoder
from impacket import dot11

import helpers
import interface

from Extensions import wps


WIFI_DIRECT_SSID = "DIRECT-"


class Scanner(object):

    def __init__(self, iface):
        self.iface = iface
        mac_address = "00:de:ad:be:ef:00"
        raw_mac_address = helpers.get_buffer_from_string_mac_address(mac_address)
        tx = Transmitter(iface, raw_mac_address)
        try:
            tx.start()
            Receiver(iface, mac_address)
        finally:
            tx.terminate()


class Transmitter(Process):
    """P2P (Wi-Fi Direct) Transmitter handles P2P discovery frame transmission."""

    def __init__(self, iface, mac_address):
        Process.__init__(self)
        self.pd = pcapy.open_live(iface, helpers.PCAP_SNAPLEN, helpers.PCAP_PROMISCOUS, helpers.PCAP_TIMEOUT)
        self.mac_address = mac_address
        self.iface = iface
        self.channel = interface.get_interface_channel(self.iface)

    def get_radiotap_header(self):
        """Returns a radiotap header buffer for frame injection."""
        buff = str()
        buff += "\x00\x00"  # Version
        buff += "\x0b\x00"  # Header length
        buff += "\x04\x0c\x00\x00"  # Bitmap
        buff += "\x6c"  # Rate
        buff += "\x0c"  # TX Power
        buff += "\x01"  # Antenna
        return buff

    def get_probe_request_frame(self, seq):
        """Returns management probe request frame header."""
        buff = str()
        buff += self.get_radiotap_header()
        buff += "\x40\x00"  # Frame Control - Management - Probe Request
        buff += "\x00\x00"  # Duration
        buff += "\xff\xff\xff\xff\xff\xff"  # Destination Address- Broadcast
        buff += self.mac_address  # Source Address
        buff += "\xff\xff\xff\xff\xff\xff"  # BSSID Address - Broadcast
        # buffer += "\x00\x00"  # Sequence Control
        buff += "\x00" + struct.pack("B", seq)[0]  # Sequence Control
        # SSID IE
        buff += "\x00"
        buff += "\x00"
        # Supported Rates IE
        buff += "\x01"
        buff += "\x08"
        buff += "\x0c\x12\x18\x24\x30\x48\x60\x6c"
        # DS Parameter Set IE
        buff += "\x03"
        buff += "\x01"
        buff += struct.pack("B", self.channel)[0]
        return buff

    def run(self):
        """Transmit frames forever."""
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        seq = 0  # TODO: Fix how we are handling sequence numbers
        frame = self.get_probe_request_frame(seq)
        while True:
            current_channel = interface.get_interface_channel(self.iface)
            if current_channel != self.channel:
                self.channel = current_channel
                frame = self.get_probe_request_frame(seq % 255)
            self.pd.sendpacket(frame)
            seq += 1
            time.sleep(0.100)


class Receiver(object):
    """P2P (Wi-Fi Direct) Receiver class handles P2P discovery frame reception and parsing."""

    def __init__(self, iface, mac_address):
        self.devices = dict()
        self.pd = pcapy.open_live(iface, helpers.PCAP_SNAPLEN, helpers.PCAP_PROMISCOUS, helpers.PCAP_TIMEOUT)
        bpf_filter = "(type mgt subtype probe-resp) and (wlan addr1 %s)" % mac_address
        self.pd.setfilter(bpf_filter)
        datalink = self.pd.datalink()
        if datalink == helpers.PCAP_DLT_IEEE802_11:
            self.decoder = ImpactDecoder.Dot11Decoder()
        elif datalink == helpers.PCAP_DLT_IEEE802_11_RADIOTAP:
            self.decoder = ImpactDecoder.RadioTapDecoder()
        else:
            raise Exception("Invalid datalink.")
        self.run()

    def run(self):
        """Receive and process frames forever."""
        while True:
            try:
                hdr, frame = self.pd.next()
                if frame:
                    self.process_probe_response_frame(frame)
            except Exception, e:
                print "Exception: %s" % str(e)
            except KeyboardInterrupt:
                print "Sniffing Process: Caught CTRL+C. Exiting..."
                break

    def process_probe_response_frame(self, data):
        """Process Probe Response frame searching for WPS and P2P IEs and storing information."""
        self.decoder.decode(data)
        frame_control = self.decoder.get_protocol(dot11.Dot11)
        if frame_control.get_type() != dot11.Dot11Types.DOT11_TYPE_MANAGEMENT or \
           frame_control.get_subtype() != dot11.Dot11Types.DOT11_SUBTYPE_MANAGEMENT_PROBE_RESPONSE:
            return

        mgt_frame = self.decoder.get_protocol(dot11.Dot11ManagementFrame)
        device_mac = helpers.get_string_mac_address_from_array(mgt_frame.get_source_address())

        probe_response_frame = self.decoder.get_protocol(dot11.Dot11ManagementProbeResponse)
        ssid = probe_response_frame.get_ssid()
        channel = probe_response_frame.get_ds_parameter_set()

        security = helpers.get_security(probe_response_frame)

        if device_mac not in self.devices.keys():
            self.devices[device_mac] = list()
            vs_list = probe_response_frame.get_vendor_specific()
            for item in vs_list:
                oui, data = item
                vs_type = data[0]
                length = struct.pack("B", len(oui + data))
                raw_data = wps.WPSInformationElement.VENDOR_SPECIFIC_IE_ID + length + oui + data
                if oui == wps.WPSInformationElement.WPS_OUI and vs_type == wps.WPSInformationElement.WPS_OUI_TYPE:
                    print "BSSID: %s" % device_mac
                    print "SSID: %s" % ssid
                    print "Channel: %d" % channel
                    print "Security: %s" % security
                    print "-" * 20
                    ie = wps.WPSInformationElement(raw_data)
                    for element in ie.get_elements():
                        k, v = element
                        if all(c in string.printable for c in v):
                            print "%s: %s" % (string.capwords(k), v)
                        else:
                            print "%s: %r" % (string.capwords(k), v)
                    print "-" * 70


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(-1)
    iface = sys.argv[1]
    Scanner(iface)
