import asyncio
import binascii
import socket
import struct
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from io import BytesIO

import aiohttp
import aiohttp_jinja2
import jinja2
from aiohttp.web import Response

# TODO autodetect these
# You can get this with 'ip addr'
MY_MAC = '4c:cc:6a:22:56:86'
MY_IP = '10.0.0.22'

MY_MAC = MY_MAC.upper()
SONOS_NAME = socket.gethostname().capitalize()
SONOS_ID = 'RINCON_'+MY_MAC.upper().replace(':','')+'01400'
SUBSCRIPTION_TIMEOUT = 3600
FIRMWARE_VERSION = '34.16-37101'
FIRMWARE_DISPLAY_VERSION = '7.1'
SERVER_HEADER = 'Linux UPnP/1.0 Sonos/{} (ZPS5)'.format(FIRMWARE_VERSION)
SONOS_HOUSEHOLD = 'Sonos_AafT5QbaoptKSoEB7VzvHfC5Uu'  # TODO Autodiscover this

async def do_hello():
    things = """NOTIFY * HTTP/1.1
HOST: 239.255.255.250:1900
CACHE-CONTROL: max-age = 1800
LOCATION: http://{ip}:1400/xml/device_description.xml
NT: urn:schemas-upnp-org:device:ZonePlayer:1
NTS: ssdp:alive
SERVER: {server}
USN: uuid:{sonos_id}::urn:schemas-upnp-org:device:ZonePlayer:1
X-RINCON-HOUSEHOLD: {household}
X-RINCON-BOOTSEQ: 2
X-RINCON-WIFIMODE: 0
X-RINCON-VARIANT: 0""".format(ip=MY_IP, sonos_id=SONOS_ID, server=SERVER_HEADER, household=SONOS_HOUSEHOLD)

    MCAST_GRP = "239.255.255.250"
    MCAST_PORT = 1900

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    # UPnP v1.0 requires a TTL of 4
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, struct.pack("B", 4))

    while True:
        sock.sendto(things.encode('utf-8'), (MCAST_GRP, MCAST_PORT))
        await asyncio.sleep(1)

asyncio.ensure_future(do_hello())

next_sid = 0

def generate_sid():
    global next_sid
    res = "{}_sub{:010}".format(SONOS_ID, next_sid)
    next_sid += 1
    return res

class Subscription():
    def __init__(self, service, callback_url):
        self.id = generate_sid()
        self.service = service
        self.callback_url = callback_url
        self.seq = 0

    async def notify(self):
        ns = 'urn:schemas-upnp-org:event-1-0'

        root = ET.Element('{%s}propertyset' % ns)
        evented_variables = 0

        for name, var in self.service._variables.items():
            if not var.is_evented:
                continue

            e = ET.SubElement(root, '{%s}property' % ns)
            elem = ET.SubElement(e, name)
            elem.text = str(self.service._variable_values[name])
            evented_variables += 1

        if evented_variables > 0:
            xml = ET.tostring(root, encoding='utf-8')
            print(xml)
            resp = await aiohttp.request('NOTIFY', self.callback_url, data=xml, headers={
                'CONTENT-TYPE': 'text/xml',
                'NT': 'upnp:event',
                'NTS': 'upnp:propchange',
                'SID': 'uuid:'+self.id,
                'SEQ': str(self.seq),
            })
            self.seq += 1
            print(resp.status)
            print(await resp.text())


NS_SOAP_ENV = "{http://schemas.xmlsoap.org/soap/envelope/}"
NS_SOAP_ENC = "{http://schemas.xmlsoap.org/soap/encoding/}"
NS_XSI = "{http://www.w3.org/1999/XMLSchema-instance}"
NS_XSD = "{http://www.w3.org/1999/XMLSchema}"

SOAP_ENCODING = "http://schemas.xmlsoap.org/soap/encoding/"

def qname(tag, ns=None):
    if not ns:
        return tag
    return "{%s}%s" % (ns, tag)

def textElement(parent, tag, namespace, text):
    """Create a subelement with text content."""
    elem = ET.SubElement(parent, qname(tag, namespace))
    elem.text = text
    return elem

UPNPERRORS = {401: 'Invalid Action',
              402: 'Invalid Args',
              501: 'Action Failed',
              600: 'Argument Value Invalid',
              601: 'Argument Value Out of Range',
              602: 'Optional Action Not Implemented',
              603: 'Out Of Memory',
              604: 'Human Intervention Required',
              605: 'String Argument Too Long',
              606: 'Action Not Authorized',
              607: 'Signature Failure',
              608: 'Signature Missing',
              609: 'Not Encrypted',
              610: 'Invalid Sequence',
              611: 'Invalid Control URL',
              612: 'No Such Session', }

def decode_result(element):
    type = element.get('{http://www.w3.org/1999/XMLSchema-instance}type')
    if type is not None:
        try:
            prefix, local = type.split(":")
            if prefix == 'xsd':
                type = local
        except ValueError:
            pass

    if type in ("integer", "int"):
        return int(element.text)
    elif type in ("float", "double"):
        return float(element.text)
    elif type == "boolean":
        return element.text == "true"
    else:
        return element.text or ""

def build_soap_error(status, description='without words'):
    """ builds an UPnP SOAP error msg
    """
    root = ET.Element('s:Fault')
    textElement(root, 'faultcode', None, 's:Client')
    textElement(root, 'faultstring', None, 'UPnPError')
    e = ET.SubElement(root, 'detail')
    e = ET.SubElement(e, 'UPnPError')
    e.attrib['xmlns'] = 'urn:schemas-upnp-org:control-1-0'
    textElement(e, 'errorCode', None, str(status))
    textElement(e, 'errorDescription', None, UPNPERRORS.get(status, description))
    xml = build_soap_call(None, root, encoding=None)
    print(xml)
    return Response(
        status=500,
        body=xml,
        headers={
            'Content-Type': 'application/xml',
        }
    )

def build_soap_call(method, arguments, is_response=False,
                                       encoding=SOAP_ENCODING,
                                       envelope_attrib=None,
                                       typed=None):
    """ create a shell for a SOAP request or response element
        - set method to none to omitt the method element and
          add the arguments directly to the body (for an error msg)
        - arguments can be a dict or an ET.Element
    """
    envelope = ET.Element("s:Envelope")
    if envelope_attrib:
        # :fixme: ensure there is no xmlns defined here
        for n in envelope_attrib:
            envelope.attrib.update({n[0]: n[1]})
    else:
        envelope.attrib.update({'s:encodingStyle': "http://schemas.xmlsoap.org/soap/encoding/"})
        # :fixme: remove explict xmlns attribute
        envelope.attrib.update({'xmlns:s': "http://schemas.xmlsoap.org/soap/envelope/"})

    body = ET.SubElement(envelope, "s:Body")

    if method:
        # append the method call
        if is_response is True:
            method += "Response"
        re = ET.SubElement(body, method)
        if encoding:
            re.set(NS_SOAP_ENV + "encodingStyle", encoding)
    else:
        re = body

    # append the arguments
    if isinstance(arguments, dict):
        type_map = {str: 'xsd:string',
                    int: 'xsd:int',
                    float: 'xsd:float',
                    bool: 'xsd:boolean'}

        for arg_name, arg_val in arguments.iteritems():
            arg_type = type_map[type(arg_val)]
            if arg_type == 'xsd:int' or arg_type == 'xsd:float':
                arg_val = str(arg_val)
            if arg_type == 'xsd:boolean':
                if arg_val == True:
                    arg_val = '1'
                else:
                    arg_val = '0'

            e = ET.SubElement(re, arg_name)
            if typed and arg_type:
                if not isinstance(type, ET.QName):
                    arg_type = ET.QName("http://www.w3.org/1999/XMLSchema", arg_type)
                e.set(NS_XSI + "type", arg_type)
            e.text = arg_val
    else:
        if arguments == None:
            arguments = {}
        re.append(arguments)

    f = BytesIO()
    ET.ElementTree(envelope).write(f, encoding='utf-8', xml_declaration=True)
    return f.getvalue()

class Service():
    def __init__(self, name, router):
        self.name = name
        self.subscriptions = {}
        app.router.add_route('POST', '/'+name+'/Control', self.handle_control)
        app.router.add_route('SUBSCRIBE', '/'+name+'/Event', self.handle_subscribe)
        app.router.add_route('UNSUBSCRIBE', '/'+name+'/Event', self.handle_unsubscribe)
        self._variable_values = {}
        self._variables = {}

        selftype = type(self)
        for attr in dir(selftype):
            v = getattr(selftype, attr)
            if isinstance(v, Variable):
                self._variable_values[attr] = v.default
                self._variables[attr] = v
                print(attr)

        self._pending_event = False

    async def handle_control(self, request):
        def print_c(e):
            for c in e.getchildren():
                print(c, c.tag)
                print_c(c)
        print(request.path)
        print(await request.text())
        tree = ET.fromstring(await request.text())

        body = tree.find('{http://schemas.xmlsoap.org/soap/envelope/}Body')
        method = body.getchildren()[0]
        methodName = method.tag

        if methodName.startswith('{') and methodName.rfind('}') > 1:
            _, methodName = methodName[1:].split('}')

        kwargs = {}
        for child in method.getchildren():
            kwargs[child.tag] = decode_result(child)
        methodName = methodName.lower()
        print(methodName, kwargs)
        func = getattr(self, 'handle_soap_'+methodName, None)
        if func is None:
            return build_soap_error(401)

        res = func(**kwargs)

        return Response(
            text=res,
            headers={
                'Content-Type': 'application/xml',
            }
        )

    async def handle_subscribe(self, request):
        if request.headers.get('NT') == 'upnp:event':
            cb = request.headers.get('CALLBACK')
            assert cb is not None
            cb = cb.strip()
            assert cb[0] == '<'
            assert cb[-1] == '>'
            cb = cb[1:-1]

            s = Subscription(self, cb)
            self.subscriptions[s.id] = s

            asyncio.ensure_future(s.notify())

            return Response(
                headers={
                    "SID": 'uuid:' + s.id,
                    "TIMEOUT": "Second-{}".format(SUBSCRIPTION_TIMEOUT)
                }
            )
        else: # Renewal
            sid = request.headers.get('SID')
            if sid in self.subscriptions:
                return Response()
            else:
                return Response(status=412)

    async def handle_unsubscribe(self, request):
        sid = request.headers.get('SID')
        if sid in self.subscriptions:
            self.subscriptions.pop(sid)
            return Response()
        else:
            return Response(status=412)

    def _send_events(self):
        if not self._pending_event:
            return
        self._pending_event = False
        print("Sending events!!!")

    def _get_variable(self, name):
        return self._variable_values[name]

    def _set_variable(self, name, value):
        var = self._variables[name]
        self._variable_values[name] = value
        if var.is_evented and not self._pending_event:
            self._pending_event = True
            self._send_events()

class Variable(object):
    def snoop_name(self, objtype):
        for attr in dir(objtype):
            if getattr(objtype, attr) is self:
                return attr

    def __init__(self, is_evented=False, default=None):
        self.is_evented = is_evented
        self.default = default

    def __get__(self, obj, objtype):
        if obj is None:
            return self
        name = self.snoop_name(objtype)
        print('Retrieving', name)
        return obj._get_variable(name)

    def __set__(self, obj, val):
        name = self.snoop_name(type(obj))
        print('Updating' , name, 'to', val)
        obj._set_variable(name, val)

class DevicePropertiesService(Service):
    ZoneName = Variable(is_evented=True, default=SONOS_NAME)
    Icon = Variable(is_evented=True, default='x-rincon-roomicon:bathroom')
    Configuration = Variable(is_evented=True, default='1')
    Invisible = Variable(is_evented=True, default='0')
    IsZoneBridge = Variable(is_evented=True, default='0')
    WirelessMode = Variable(is_evented=True, default='0')
    WirelessLeafOnly = Variable(is_evented=True, default='0')
    HasConfiguredSSID = Variable(is_evented=True, default='1')
    ChannelFreq = Variable(is_evented=True, default='2412')
    BehindWifiExtender = Variable(is_evented=True, default=' 0')
    WifiEnabled = Variable(is_evented=True, default='1')
#    SettingsReplicationState = Variable(is_evented=True, default='RINCON_B8E93724C80001400,0,RINCON_FFFFFFFFFFFF99999,0,RINCON_B8E93724C80001400,0,RINCON_B8E93724C80001400,0,RINCON_B8E93724C80001400,2,RINCON_B8E93724C80001400,0,RINCON_B8E93724C80001400,0,RINCON_FFFFFFFFFFFF99999,0,RINCON_B8E93724C80001400,7,RINCON_FFFFFFFFFFFF99999,0,RINCON_B8E93724C80001400,0,RINCON_B8E93724C80001400,1')
    SettingsReplicationState = Variable(is_evented=True, default='')
    SecureRegState = Variable(is_evented=True, default='2')
    ChannelMapSet = Variable(is_evented=True, default='')
    HTSatChanMapSet = Variable(is_evented=True, default='')
    HTBondedZoneCommitState = Variable(is_evented=True, default='0')
    Orientation = Variable(is_evented=True, default='0')
    LastChangedPlayState = Variable(is_evented=True, default='')
    AvailableRoomCalibration = Variable(is_evented=True, default='')
    RoomCalibrationState = Variable(is_evented=True, default='4')
    ConfigMode = Variable(is_evented=True, default='')

    def __init__(self, router):
        Service.__init__(self, 'DeviceProperties', router)

    def handle_soap_getzoneinfo(self, **kwargs):
        return """
        <s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
        <s:Body><u:GetZoneInfoResponse xmlns:u="urn:schemas-upnp-org:service:DeviceProperties:1">
            <SerialNumber>{mac_hyphens}:3</SerialNumber>
            <SoftwareVersion>{v}</SoftwareVersion>
            <DisplaySoftwareVersion>{dv}</DisplaySoftwareVersion>
            <HardwareVersion>1.17.4.1-2</HardwareVersion>
            <IPAddress>{my_ip}</IPAddress>
            <MACAddress>{mac}</MACAddress>
            <CopyrightInfo>blahblah</CopyrightInfo>
            <ExtraInfo>OTP: 1.1.1(1-17-4-zp5s-2.1)</ExtraInfo>
            <HTAudioIn>0</HTAudioIn>
            <Flags>0</Flags>
        </u:GetZoneInfoResponse></s:Body></s:Envelope>
    """.format(my_ip=MY_IP, mac=MY_MAC, mac_hyphens=MY_MAC.replace(':', '-'), v=FIRMWARE_VERSION, dv=FIRMWARE_DISPLAY_VERSION)

class GroupManagementService(Service):
    GroupCoordinatorIsLocal = Variable(default=1, is_evented=True)
    LocalGroupUUID = Variable(default=SONOS_ID+':0', is_evented=True)

    def __init__(self, router):
        Service.__init__(self, 'GroupManagement', router)

class AVTransportService(Service):
    def __init__(self, router):
        Service.__init__(self, 'MediaRenderer/AVTransport', router)
    def handle_soap_gettransportinfo(self, **kwargs):
        return """
            <s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>
                <u:GetTransportInfoResponse xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">
                    <CurrentTransportState>STOPPED</CurrentTransportState>
                    <CurrentTransportStatus>OK</CurrentTransportStatus>
                    <CurrentSpeed>1</CurrentSpeed>
                </u:GetTransportInfoResponse>
            </s:Body></s:Envelope>"""

class ContentDirectoryService(Service):
    SystemUpdateID = Variable(is_evented=True, default='2')
    ContainerUpdateIDs = Variable(is_evented=True, default='AI:,1')
    ShareIndexInProgress = Variable(is_evented=True, default='0')
    ShareIndexLastError = Variable(is_evented=True, default='None')
    FavoritesUpdateID = Variable(is_evented=True, default=SONOS_ID+',0')
    FavoritePresetsUpdateID = Variable(is_evented=True, default=SONOS_ID+',0')
    RadioFavoritesUpdateID = Variable(is_evented=True, default=SONOS_ID+',0')
    RadioLocationUpdateID = Variable(is_evented=True, default=SONOS_ID+',39')
    SavedQueuesUpdateID = Variable(is_evented=True, default=SONOS_ID+',3')
    ShareListUpdateID = Variable(is_evented=True, default=SONOS_ID+',0')

    def __init__(self, router):
        Service.__init__(self, 'MediaServer/ContentDirectory', router)

    def handle_soap_browse(self, **kwargs):
        return """
        <s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
            <s:Body>
                <u:BrowseResponse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">
                <Result>
                    &lt;DIDL-Lite xmlns:dc=&quot;http://purl.org/dc/elements/1.1/&quot; xmlns:upnp=&quot;urn:schemas-upnp-org:metadata-1-0/upnp/&quot; xmlns:r=&quot;urn:schemas-rinconnetworks-com:metadata-1-0/&quot; xmlns=&quot;urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/&quot;&gt;&lt;item id=&quot;AI:0&quot; parentID=&quot;AI:&quot; restricted=&quot;true&quot;&gt;&lt;upnp:class&gt;object.item.audioItem&lt;/upnp:class&gt;&lt;dc:title&gt;SonosCast&lt;/dc:title&gt;&lt;res protocolInfo=&quot;x-rincon-stream:*:*:*&quot;&gt;x-rincon-stream:{}&lt;/res&gt;&lt;/item&gt;&lt;/DIDL-Lite&gt;
                </Result>
                <NumberReturned>1</NumberReturned>
                <TotalMatches>1</TotalMatches>
                <UpdateID>1</UpdateID>
                </u:BrowseResponse>
            </s:Body>
        </s:Envelope>
""".format(SONOS_ID)

class QueueService(Service):
    def __init__(self, router):
        Service.__init__(self, 'MediaRenderer/Queue', router)

class RenderingControlService(Service):
    def __init__(self, router):
        Service.__init__(self, 'MediaRenderer/RenderingControl', router)

class ZoneGroupTopologyService(Service):
    ZoneGroupState = Variable(is_evented=True, default='''<ZoneGroups>
        <ZoneGroup Coordinator="{my_id}" ID="{my_id}:0">
            <ZoneGroupMember
                UUID="{my_id}"
                Location="http://{my_ip}:1400/xml/device_description.xml"
                ZoneName="{my_name}"
                Icon="x-rincon-roomicon:bathroom"
                Configuration="1"
                SoftwareVersion="{firmware_version}"
                MinCompatibleVersion="33.0-00000"
                LegacyCompatibleVersion="25.0-00000"
                BootSeq="2"
                WirelessMode="0"
                WirelessLeafOnly="0"
                HasConfiguredSSID="1"
                ChannelFreq="2412"
                BehindWifiExtender="0"
                WifiEnabled="1"
                Orientation="0"
                RoomCalibrationState="4"
                SecureRegState="2"/>
        </ZoneGroup></ZoneGroups>'''.format(my_id=SONOS_ID, my_ip=MY_IP, my_name=SONOS_NAME, firmware_version=FIRMWARE_VERSION))
    ThirdPartyMediaServersX = Variable(is_evented=True, default='')
    AvailableSoftwareUpdate = Variable(is_evented=True, default='''
        <UpdateItem xmlns="urn:schemas-rinconnetworks-com:update-1-0"
            Type="Software"
            Version="{v}"
            UpdateURL="http://update-firmware.sonos.com/firmware/Gold/{v}-v{dv}-bnyrye-GA1/^{v}"
            DownloadSize="0"
            ManifestURL="http://update-firmware.sonos.com/firmware/Gold/{v}-v{dv}-ktwktj-SP1/update_1481660983.upm"/>
        '''.format(v=FIRMWARE_VERSION, dv=FIRMWARE_DISPLAY_VERSION))
    AlarmRunSequence = Variable(is_evented=True, default=SONOS_ID+':2:0')
    ZoneGroupName = Variable(is_evented=True, default='')
    ZoneGroupID = Variable(is_evented=True, default='')
    ZonePlayerUUIDsInGroup = Variable(is_evented=True, default='')
    def __init__(self, router):
        Service.__init__(self, 'ZoneGroupTopology', router)



class AudioInService(Service):
    AudioInputName = Variable(is_evented=True, default='SonosCast')
    Icon = Variable(is_evented=True, default='AudioComponent')
    LineInConnected = Variable(is_evented=True, default='1')
    LeftLineInLevel = Variable(is_evented=True, default='1')
    RightLineInLevel = Variable(is_evented=True, default='1')

    def __init__(self, router):
        Service.__init__(self, 'AudioIn', router)
        self.proc = None

    def handle_soap_starttransmissiontogroup(self, CoordinatorID):
        print('StartTransmissionToGroup', CoordinatorID)
        if self.proc is None:
            self.proc = subprocess.Popen(["./stream"])
        return '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><u:StartTransmissionToGroupResponse xmlns:u="urn:schemas-upnp-org:service:AudioIn:1"><CurrentTransportSettings>225.238.76.46:6982,{my_ip}:6980:6981,{my_id}</CurrentTransportSettings></u:StartTransmissionToGroupResponse></s:Body></s:Envelope>'.format(my_ip=MY_IP, my_id=SONOS_ID)

    def handle_soap_stoptransmissiontogroup(self, CoordinatorID):
        if self.proc:
            self.proc.kill()
        self.proc = None
        print('StopTransmissionToGroup', CoordinatorID)

app = aiohttp.web.Application()

@aiohttp_jinja2.template('device_description.xml')
async def get_xml(request):
    print('GET /xml/device_description.xml')
    return {
        'my_ip': MY_IP,
        'my_id': SONOS_ID,
        'mac_hyphens': MY_MAC.replace(':', '-'),
        'FIRMWARE_VERSION': FIRMWARE_VERSION,
        'FIRMWARE_DISPLAY_VERSION': FIRMWARE_DISPLAY_VERSION,
    }

app.router.add_get('/xml/device_description.xml', get_xml)
aiohttp_jinja2.setup(app, loader=jinja2.FileSystemLoader('templates'))

DevicePropertiesService(app.router)
GroupManagementService(app.router)
QueueService(app.router)
AVTransportService(app.router)
RenderingControlService(app.router)
ContentDirectoryService(app.router)
ZoneGroupTopologyService(app.router)
AudioInService(app.router)

app.router.add_static('/', 'webroot/')
aiohttp.web.run_app(app, port=1400)
