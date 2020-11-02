import sys
import json
import socket
import base64
import logging
import datetime
import traceback
import gevent
from . import util
from ws4py.client.geventclient import WebSocketClient


# provides an interface to the rhizo-server message server
class MessageClient(object):

    def __init__(self, controller):
        self._controller = controller
        self._web_socket = None
        self._outgoing_messages = []
        self._message_handlers = []  # user-defined message handlers

    def connect(self):
        gevent.spawn(self.web_socket_listener)
        gevent.spawn(self.web_socket_sender)
        gevent.spawn(self.ping_web_socket)

    # returns True if websocket is connected to server
    def connected(self):
        return self._web_socket is not None

    # send a generic message to the server
    def send(self, type, parameters, channel=None, folder=None, prepend=False):
        message_struct = {
            'type': type,
            'parameters': parameters
        }
        if folder:
            message_struct['folder'] = folder
        if channel:
            message_struct['channel'] = channel
        self.send_message_struct_to_server(message_struct, prepend)

    # send an email (to up to five addresses)
    def send_email(self, email_addresses, subject, body):
        self.send('send_email', {
            'emailAddresses': email_addresses,
            'subject': subject,
            'body': body,
        })

    # send a text message (to up to five phone numbers)
    def send_sms(self, phone_numbers, message):
        self.send('send_text_message', {
            'phoneNumbers': phone_numbers,
            'message': message,
        })

    # add a custom handler for messages from server
    def add_handler(self, message_handler):
        self._message_handlers.append(message_handler)

    # ======== internal functions ========

    # initiate a websocket connection with the server
    def connect_web_socket(self):
        config = self._controller.config
        if 'secure_server' in config:
            secure_server = config.secure_server
        else:
            host_name = config.server_name.split(':')[0]
            secure_server = host_name != 'localhost' and host_name != '127.0.0.1'
        protocol = 'wss' if secure_server else 'ws'
        if config.get('old_auth', False):
            headers = None
        else:
            user_name = self._controller.VERSION + '.' + self._controller.BUILD  # send client version as user name
            password = config.secret_key  # send secret key as password
            headers = [('Authorization', 'Basic %s' % base64.b64encode(('%s:%s' % (user_name, password)).encode()).decode())]
        ws = WebSocketClient(protocol + '://' + config.server_name + '/api/v1/websocket', protocols=['http-only'], headers=headers)
        try:
            ws.connect()
            logging.debug('opened websocket connection to server')
        except Exception as e:
            logging.debug(str(e))
            logging.warning('error connecting to websocket server')
            ws = None
        return ws

    # handle an incoming message from the websocket
    def process_web_socket_message(self, message):
        message_struct = json.loads(str(message))

        # process the message
        if 'type' in message_struct and 'parameters' in message_struct:
            type = message_struct['type']
            params = message_struct['parameters']
            channel = message_struct.get('channel')
            response_message = None
            if type == 'get_config' or type == 'getConfig':
                response_message = self.config_message(params['names'].split(','))
            elif type == 'set_config' or type == 'setConfig':
                self.set_config(params)
            else:
                message_used = False
                if not message_used and self._message_handlers:
                    for handler in self._message_handlers:
                        if hasattr(handler, 'handle_message'):
                            handler.handle_message(type, params)
                        else:
                            handler(type, params)
            if response_message:
                if channel:
                    response_message['channel'] = channel
                self.send_message_struct_to_server(response_message)

    # send a websocket message to the server
    def send_message_struct_to_server(self, message_struct, prepend=False, timestamp=None):
        timestamp = datetime.datetime.utcnow()
        if prepend:
            self._outgoing_messages = [(timestamp, message_struct)] + self._outgoing_messages
        else:
            self._outgoing_messages.append((timestamp, message_struct))

    # send a websocket message to the server subscribing to messages intended for this controller
    # note: these messages are prepended to the queue, so that we're authenticated for everything else in the queue
    def send_init_socket_messages(self):
        config = self._controller.config
        params = {
            'authCode': util.build_auth_code(config.secret_key),
            'version': self._controller.VERSION + ':' + self._controller.BUILD
        }
        if 'name' in config:
            params['name'] = config.name
        self.send('subscribe', {
            'subscriptions': [  # note: this subscription listens to all message types
                {
                    'folder': 'self',
                    'include_children': config.get('subscribe_children', False),
                }
            ]
        }, prepend=True)
        if config.get('old_auth', False):
            self.send('connect', params, prepend=True)  # add to queue after subscribe so send before; fix(soon): revisit this
        logging.info('controller connected/re-connected')

    # get a configuration setting as a message
    def config_message(self, names):
        return {
            'type': 'config',
            'parameters': {name: self._controller.config.get(name, '') for name in names},
        }

    # update the config file using a dictionary of config entries
    # fix(soon): this is out of date (doesn't fit current config file format); rework it or remove it
    def set_config(self, params):
        output_lines = []
        input_file = open(self._controller._config_relative_file_name)
        for line in input_file:
            parts = line.split()
            if parts and parts[0] in params:
                line = '%s %s\n' % (parts[0], params[parts[0]])
                output_lines.append(line)
            else:
                output_lines.append(line)
        input_file.close()
        output_file = open(self._controller._config_relative_file_name, 'w')
        for line in output_lines:
            output_file.write(line)
        output_file.close()
        self._controller.load_config()
        self._controller.show_config()

    # runs as a greenlet that maintains a websocket connection with the server
    def web_socket_listener(self):
        while True:
            try:
                if self._web_socket:
                    try:
                        message = self._web_socket.receive()
                    except:
                        message = None
                    if message:
                        self.process_web_socket_message(message)
                    else:
                        logging.warning('disconnected (on received); reconnecting...')
                        self._web_socket = None
                        gevent.sleep(10)  # avoid fast reconnects
                gevent.sleep(0.1)
            except Exception as e:
                self._controller.error('error in web socket message listener/handler', exception = e)
                exc_type, exc_value, exc_traceback = sys.exc_info()
                logging.info(traceback.format_exception_only(exc_type, exc_value))
                stack = traceback.extract_tb(exc_traceback)
                for line in stack:
                    logging.info(line)

    # runs as a greenlet that sends queued messages to the server
    def web_socket_sender(self):
        while True:
            if self._web_socket:
                while self._outgoing_messages:
                    try:
                        if self._web_socket:  # check again, in case we closed the socket in another thread
                            (timestamp, message_struct) = self._outgoing_messages[0]
                            if timestamp > datetime.datetime.utcnow() - datetime.timedelta(minutes=5):  # discard (don't send) messages older than 5 minutes
                                self._web_socket.send(json.dumps(message_struct, separators=(',', ':')) + '\n')
                            self._outgoing_messages = self._outgoing_messages[1:]  # remove from queue after send
                    except (AttributeError, socket.error):
                        logging.debug('disconnected (on send); reconnecting...')
                        self._web_socket = None
                        break
                gevent.sleep(0.1)
            else:  # connect if not already connected
                try:
                    self._web_socket = self.connect_web_socket()
                    if self._web_socket:
                        self.send_init_socket_messages()
                    else:
                        gevent.sleep(10)
                except Exception as e:
                    logging.debug(str(e))
                    logging.warning('error connecting; will try again')
                    gevent.sleep(10)  # let's not try to reconnect too often

    # runs as a greenlet that maintains a periodically pings the web server to keep the websocket connection alive
    def ping_web_socket(self):
        while True:
            gevent.sleep(45)
            if self._web_socket:
                self.send('ping', {})