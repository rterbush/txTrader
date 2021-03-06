#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
  rtx.py
  ------

  RealTick API interface module

  Copyright (c) 2015 Reliance Systems Inc. <mkrueger@rstms.net>
  Licensed under the MIT license.  See LICENSE for details.

"""
import sys
import types
from uuid import uuid1
import ujson as json
import time
from collections import OrderedDict
from hexdump import hexdump
import pytz
import tzlocal
import datetime
from pprint import pprint

from txtrader.config import Config

CALLBACK_METRIC_HISTORY_LIMIT = 1024

TIMEOUT_TYPES = ['DEFAULT', 'ACCOUNT', 'ADDSYMBOL', 'ORDER', 'ORDERSTATUS', 'POSITION', 'TIMER']

# default RealTick orders to NYSE and Stock type
RTX_EXCHANGE='NYS'
RTX_STYPE=1

# allow disable of tick requests for testing

ENABLE_CXN_DEBUG = False

DISCONNECT_SECONDS = 30 
SHUTDOWN_ON_DISCONNECT = True 

from twisted.python import log
from twisted.python.failure import Failure
from twisted.internet.protocol import Protocol, ReconnectingClientFactory
from twisted.protocols.basic import LineReceiver 
from twisted.internet import reactor, defer
from twisted.internet.task import LoopingCall
from twisted.web import server
from socket import gethostname

class RtxClient(LineReceiver):
    delimiter = '\n'
    # set 16MB line buffer
    MAX_LENGTH = 0x1000000
    def __init__(self, rtx):
        self.rtx = rtx

    def connectionMade(self):
        self.rtx.gateway_connect(self)

    def lineReceived(self, data):
        self.rtx.gateway_receive(data)

    def lineLengthExceeded(self, line):
        self.rtx.force_disconnect('RtxClient: Line length exceeded: line=%s' % repr(line))

class RtxClientFactory(ReconnectingClientFactory):
    initialDelay = 15
    maxDelay = 60
    def __init__(self, rtx):
        self.rtx = rtx

    def startedConnecting(self, connector):
        self.rtx.output('RTGW: Started to connect.')
    
    def buildProtocol(self, addr):
        self.rtx.output('RTGW: Connected.')
        self.resetDelay()
        return RtxClient(self.rtx)

    def clientConnectionLost(self, connector, reason):
        self.rtx.output('RTGW: Lost connection.  Reason: %s' % reason)
        ReconnectingClientFactory.clientConnectionLost(self, connector, reason)
        self.rtx.gateway_connect(None)

    def clientConnectionFailed(self, connector, reason):
        self.rtx.output('Connection failed. Reason: %s' % reason)
        ReconnectingClientFactory.clientConnectionFailed(self, connector, reason)
        self.rtx.gateway_connect(None)

class API_Symbol(object):
    def __init__(self, api, symbol, client_id, init_callback):
        self.api = api
        self.id = str(uuid1())
        self.output = api.output
        self.clients = set([client_id])
        self.callback = init_callback
        self.symbol = symbol
        self.fullname = ''
        self.bid = 0.0
        self.bid_size = 0
        self.ask = 0.0
        self.ask_size = 0
        self.last = 0.0
        self.size = 0
        self.volume = 0
        self.close = 0.0
        self.vwap = 0.0
        self.high = 0.0
        self.low = 0.0
        self.rawdata = ''
        self.api.symbols[symbol] = self
        self.last_quote = ''
        self.output('API_Symbol %s %s created for client %s' %
                    (self, symbol, client_id))
        self.output('Adding %s to watchlist' % self.symbol)
        self.cxn = api.cxn_get('TA_SRV', 'LIVEQUOTE')
        cb = API_Callback(self.api, self.cxn.id, 'init_symbol', RTX_LocalCallback(self.api, self.init_handler), self.api.callback_timeout['ADDSYMBOL'])
        self.cxn.request('LIVEQUOTE', '*', "DISP_NAME='%s'" % symbol, cb)

    def __str__(self):
        return 'API_Symbol(%s bid=%s bidsize=%d ask=%s asksize=%d last=%s size=%d volume=%d close=%s vwap=%s clients=%s' % (self.symbol, self.bid, self.bid_size, self.ask, self.ask_size, self.last, self.size, self.volume, self.close, self.vwap, self.clients)

    def __repr__(self):
        return str(self)

    def export(self):
        ret = {
            'symbol': self.symbol,
            'last': self.last,
            'size': self.size,
            'volume': self.volume,
            'close': self.close,
            'vwap': self.vwap,
            'fullname': self.fullname
        }
        if self.api.enable_high_low: 
          ret['high'] = self.high
          ret['low'] = self.low
        if self.api.enable_ticker:
          ret['bid'] = self.bid
          ret['bidsize'] = self.bid_size
          ret['ask'] = self.ask
          ret['asksize'] = self.ask_size
        return ret

    def add_client(self, client):
        self.output('API_Symbol %s %s adding client %s' %
                    (self, self.symbol, client))
        self.clients.add(client)

    def del_client(self, client):
        self.output('API_Symbol %s %s deleting client %s' %
                    (self, self.symbol, client))
        self.clients.discard(client)
        if not self.clients:
            self.output('Removing %s from watchlist' % self.symbol)
            # TODO: stop live updates of market data from RTX

    def update_quote(self):
        quote = 'quote.%s:%s %d %s %d' % (
            self.symbol, self.bid, self.bid_size, self.ask, self.ask_size)
        if quote != self.last_quote:
            self.last_quote = quote
            self.api.WriteAllClients(quote)

    def update_trade(self):
        self.api.WriteAllClients('trade.%s:%s %d %d' % (
            self.symbol, self.last, self.size, self.volume))

    def init_handler(self, data):
        data = json.loads(data)
        self.output('API_Symbol init: %s' % data)
        self.parse_fields(None, data[0])
        self.rawdata = data[0]
        for k,v in self.rawdata.items():
            if v.startswith('Error '):
                self.rawdata[k]=''
        if self.api.symbol_init(self):
            self.cxn = self.api.cxn_get('TA_SRV', 'LIVEQUOTE')
            fields = 'TRDPRC_1,TRDVOL_1,ACVOL_1'
            if self.api.enable_ticker:
                fields += ',BID,BIDSIZE,ASK,ASKSIZE'
            if self.api.enable_high_low:
                fields += ',HIGH_1,LOW_1'
            self.cxn.advise('LIVEQUOTE', fields, "DISP_NAME='%s'" % self.symbol, self.parse_fields)

    def parse_fields(self, cxn, data):
        trade_flag = False
        quote_flag = False
        pid = 'API_Symbol(%s)' % self.symbol
 
        if data == None:
            self.api.force_disconnect('LIVEQUOTE Advise has been terminated by API for %s' % pid)
            return

        if 'TRDPRC_1' in data.keys():
            self.last = self.api.parse_tql_float(data['TRDPRC_1'], pid, 'TRDPRC_1')
            trade_flag = True
        if 'HIGH_1' in data.keys():
            self.high = self.api.parse_tql_float(data['HIGH_1'], pid, 'HIGH_1')
            trade_flag = True
        if 'LOW_1' in data.keys():
            self.low = self.api.parse_tql_float(data['LOW_1'], pid, 'LOW_1')
            trade_flag = True
        if 'TRDVOL_1' in data.keys():
            self.size = self.api.parse_tql_int(data['TRDVOL_1'], pid, 'TRDVOL_1')
            trade_flag = True
        if 'ACVOL_1' in data.keys():
            self.volume = self.api.parse_tql_int(data['ACVOL_1'], pid, 'ACVOL_1')
            trade_flag = True
        if 'BID' in data.keys():
            self.bid = self.api.parse_tql_float(data['BID'], pid, 'BID')
            if self.bid and 'BIDSIZE' in data.keys():
                self.bidsize = self.api.parse_tql_int(data['BIDSIZE'], pid, 'BIDSIZE')
            else:
                self.bidsize = 0
            quote_flag = True
        if 'ASK' in data.keys():
            self.ask = self.api.parse_tql_float(data['ASK'], pid, 'ASK')
            if self.ask and 'ASKSIZE' in data.keys():
              self.asksize = self.api.parse_tql_int(data['ASKSIZE'], pid, 'ASKSIZE')
            else:
                self.asksize = 0
            quote_flag = True
        if 'COMPANY_NAME' in data.keys():
            self.fullname = self.api.parse_tql_str(data['COMPANY_NAME'], pid, 'COMPANY_NAME')
        if 'HST_CLOSE' in data.keys():
            self.close = self.api.parse_tql_float(data['HST_CLOSE'], pid, 'HST_CLOSE')
        if 'VWAP' in data.keys():
            self.vwap = self.api.parse_tql_float(data['VWAP'], pid, 'VWAP')

        if self.api.enable_ticker:
            if quote_flag:
                self.update_quote()
            if trade_flag:
                self.update_trade()

    #def update_handler(self, data):
    #    self.output('API_Symbol update: %s' % data)
    #    self.rawdata = data

class API_Order(object):
    def __init__(self, api, oid, data, callback=None):
        self.api = api
        self.oid = oid
        self.fields = data
        self.callback = callback
        self.updates = []
        self.suborders = {}

    def initial_update(self, data):
        self.update(data)
        if self.callback:
            self.callback.complete(self.render())
            self.callback = None

    def update(self, data):

        field_state = json.dumps(self.fields)
    
        if 'ORDER_ID' in data:
            order_id = data['ORDER_ID']
            if order_id in self.suborders.keys():
                if data == self.suborders[order_id]:
                    change = 'dup'
                else:
                     change = 'changed'
            else:
                change = 'new'
            self.suborders[order_id] = data
        else:
            self.api.error_handler(self.oid, 'Order Update without ORDER_ID: %s' % repr(data))
            order_id = 'unknown'
            change = 'error'

        if self.api.log_order_updates:
            self.api.output('ORDER_UPDATE: OID=%s ORDER_ID=%s %s' % (self.oid, order_id, change))

        # only apply new or changed messages to the base order; (don't move order status back in time when refresh happens)

        if change in ['new', 'changed']:
            changes={} 
            for k,v in data.items():
                ov = self.fields.setdefault(k,None)
                self.fields[k]=v
                if v!=ov:
                    changes[k]=v

            if changes:
                if self.api.log_order_updates:
                    self.api.output('ORDER_CHANGES: OID=%s ORDER_ID=%s %s' % (self.oid, order_id, repr(changes)))
                if order_id != self.oid:
                    update_type = changes['TYPE'] if 'TYPE' in changes else 'Undefined'
                    self.updates.append({'id': order_id, 'type':  update_type, 'fields': changes, 'time': time.time() })

        if json.dumps(self.fields) != field_state:
            self.api.send_order_status(self)


    def update_fill_fields(self):
        if self.fields['TYPE'] in ['UserSubmitOrder', 'ExchangeTradeOrder']:
            if 'VOLUME_TRADED' in self.fields:
                self.fields['filled'] =self.fields['VOLUME_TRADED']
            if 'ORDER_RESIDUAL' in self.fields:
                self.fields['remaining']=self.fields['ORDER_RESIDUAL']
            if 'AVG_PRICE' in self.fields: 
                self.fields['avgfillprice']=self.fields['AVG_PRICE']

    def render(self):
        # customize fields for standard txTrader order status 
        self.fields['permid']=self.fields['ORIGINAL_ORDER_ID']
        self.fields['symbol']=self.fields['DISP_NAME']
        self.fields['account']=self.api.make_account(self.fields)
        status = self.fields.setdefault('CURRENT_STATUS', 'UNDEFINED')
        otype = self.fields.setdefault('TYPE', 'Undefined')
        #print('render: permid=%s ORDER_ID=%s CURRENT_STATUS=%s TYPE=%s' % (self.fields['permid'], self.fields['ORDER_ID'], status, otype))
        #pprint(self.fields)
        if status=='PENDING': 
            self.fields['status'] = 'Submitted'
        elif status=='LIVE':
            self.fields['status'] = 'Pending'
            self.update_fill_fields()
        elif status=='COMPLETED':
            if self.is_filled():
                self.fields['status'] = 'Filled'
                if otype == 'ExchangeTradeOrder':
                    self.update_fill_fields()
            elif otype in ['UserSubmitOrder', 'UserSubmitStagedOrder', 'UserSubmitStatus', 'ExchangeReportStatus']:
                self.fields['status'] = 'Submitted'
                self.update_fill_fields()
            elif otype == 'UserSubmitCancel':
                self.fields['status'] = 'Cancelled'
            elif otype == 'UserSubmitChange':
                self.fields['status'] = 'Changed'
            elif otype == 'ExchangeAcceptOrder':
                self.fields['status'] = 'Accepted'
            elif otype == 'ExchangeTradeOrder':
                self.update_fill_fields()
            elif otype in ['ClerkReject', 'ExchangeKillOrder']:
                self.fields['status'] = 'Error'
            else:
                self.api.error_handler(self.oid, 'Unknown TYPE: %s' % otype)
                self.fields['status'] = 'Error'
        elif status=='CANCELLED':
            self.fields['status'] = 'Cancelled'
        elif status=='DELETED':
            self.fields['status'] = 'Error'
        else:
            self.api.error_handler(self.oid, 'Unknown CURRENT_STATUS: %s' % status)
            self.fields['status'] = 'Error'
            
        self.fields['updates'] = self.updates

        return self.fields

    def is_filled(self):
        return bool(self.fields['CURRENT_STATUS']=='COMPLETED' and
            self.has_fill_type() and
            'ORIGINAL_VOLUME' in self.fields and
            'VOLUME_TRADED' in self.fields and 
            self.fields['ORIGINAL_VOLUME'] == self.fields['VOLUME_TRADED'])

    def is_cancelled(self):
        return bool(self.fields['CURRENT_STATUS']=='COMPLETED' and
            'status' in self.fields and self.fields['status'] == 'Error' and
            'REASON' in self.fields and self.fields['REASON'] == 'User cancel')
 
    def has_fill_type(self):
        if self.fields['TYPE']=='ExchangeTradeOrder':
            return True
        for update_type in [update['type'] for update in self.updates]:
            if update_type =='ExchangeTradeOrder':
                return True
        return False

class API_Callback(object):
    def __init__(self, api, id, label, callable, timeout=0):
        """callable is stored and used to return results later"""
        #api.output('API_Callback.__init__() %s' % self)
        self.api = api
        self.id = id
        self.label = label
        self.started = time.time()
        self.timeout = timeout or api.callback_timeout['DEFAULT']
        self.expire = self.started + timeout
        self.callable = callable
        self.done = False
        self.data = None
        self.expired = False

    def complete(self, results):
        """complete callback by calling callable function with value of results"""
        self.elapsed = time.time() - self.started
        if not self.done:
            ret = self.format_results(results)
            if self.callable.callback.__name__ == 'sendString':
                ret = '%s.%s: %s' % (self.api.channel, self.label, ret)
            #self.api.output('API_Callback.complete(%s)' % repr(ret))
            self.callable.callback(ret)
            self.callable = None
            self.done = True
        else:
            self.api.error_handler(self.id, '%s completed after timeout: callback=%s elapsed=%.2f' % (self.label, repr(self), self.elapsed))
            self.api.output('results=%s' % repr(results))
        self.api.record_callback_metrics(self.label, int(self.elapsed * 1000), self.expired)

    def check_expire(self):
        #SElf.api.output('API_Callback.check_expire() %s' % self)
        if not self.done:
            if time.time() > self.expire:
                msg = 'error: callback expired: %s' % repr((self.id, self.label, self))
                self.api.WriteAllClients(msg)
                if self.callable.callback.__name__ == 'sendString':
                    self.callable.callback('%s.error: %s callback expired', (self.api.channel, self.label))
                else:
                    self.callable.errback(Failure(Exception(msg)))
                self.expired = True
                self.done = True

    def format_results(self, results):
        #print('format_results: label=%s results=%s' % (self.label, results))
        if self.label == 'account_data':
            results = self.format_account_data(results)
        elif self.label == 'positions':
            results = self.format_positions(results)
        elif self.label == 'orders':
            results = self.format_orders(results)
        elif self.label=='executions':
            results = self.format_executions(results)
        elif self.label == 'order_status':
            results = self.format_orders(results, self.id)

        return json.dumps(results)

    def format_account_data(self, rows):
        data = rows[0] if rows else rows
        if data and 'EXCESS_EQ' in data:
            data['_cash'] = round(float(data['EXCESS_EQ']),2)
        return data

    def format_positions(self, rows):
        # Positions should return {'ACOUNT': {'SYMBOL': QUANTITY, ...}, ...}
        positions = {}
        [positions.setdefault(a, {}) for a in self.api.accounts]
	#print('format_positions: rows=%s' % repr(rows))
        for pos in rows:
            if pos:
	        #print('format_positions: pos=%s' % repr(pos))
                account = self.api.make_account(pos)
                symbol = pos['DISP_NAME']
                positions[account].setdefault(symbol, 0)
                # if LONG positions exist, add them, if SHORT positions exist, subtract them
                for m,f in [(1,'LONGPOS'), (1, 'LONGPOS0'), (-1, 'SHORTPOS'), (-1, 'SHORTPOS0')]:
                    if f in pos:
                        positions[account][symbol] += m * int(pos[f])
        return positions

    def format_orders(self, rows, oid=None):
        for row in rows or []:
            if row:
                self.api.handle_order_response(row)
        if oid:
            results = self.api.orders[oid].render()
        else:
            results={}
            for k,v in self.api.orders.items():
                results[k] = v.render()
        return results

    def format_executions(self, rows):
        for row in rows:
            if row:
                self.api.handle_order_response(row)
        results={}
        for k,v in self.api.orders.items():
            if v.is_filled():
                results[k]=v.fields
                results[k]['updates']=v.updates
        return results

class RTX_Connection(object):
    def __init__(self, api, service, topic, enable_logging=False):
        self.api = api
        self.id = str(uuid1())
        self.service = service
        self.topic = topic
        self.key = '%s;%s' % (service, topic)
        self.last_query = ''
        self.api.output('Creating %s' % repr(self))
        self.api.cxn_register(self)
        self.api.gateway_send('connect %s %s' % (self.id, self.key))
        self.ack_pending = 'CONNECTION PENDING'
        self.log = enable_logging
        self.ack_callback = None
        self.response_pending = None
        self.response_callback = None
        self.response_rows = None
        self.status_pending = 'OnInitAck'
        self.status_callback = None
        self.update_callback = None
        self.update_handler = None
        self.connected = False
        self.on_connect_action = None
        self.update_ready()

    def __repr__(self):
        return self.__str__()

    def __str__(self):
        return '<RTX_Connection instance at %s %s %s %s>' % (hex(id(self)), self.id, self.key, self.last_query)

    def update_ready(self):
        self.ready = not(
            self.ack_pending or self.response_pending or self.status_pending or self.status_callback or self.update_callback or self.update_handler)
        #self.api.output('update_ready() %s %s' % (self.id, self.ready))
        if self.ready:
            self.api.cxn_activate(self)

    def receive(self, _type, data):
        if _type == 'ack':
            self.handle_ack(data)
        elif _type == 'response':
            self.handle_response(data)
        elif _type == 'status':
            self.handle_status(data)
        elif _type == 'update':
            self.handle_update(data)
        else:
            self.api.error_handler(self.id, 'Message Type Unexpected: %s' % data)
        self.update_ready()

    def handle_ack(self, data):
        if self.log:
            self.api.output('Ack Received: %s %s' % (self.id, data))
        if self.ack_pending:
            if data == self.ack_pending:
                self.ack_pending = None
            else:
                self.api.error_handler(self.id, 'Ack Mismatch: expected %s, got %s' % (self.ack_pending, data))
                self.handle_response_failure()
            if self.ack_callback:
                self.ack_callback.complete(data)
                self.ack_callback = None
        else:
            self.api.error_handler(self.id, 'Ack Unexpected: %s' % data)

    def handle_response(self, data):
        if self.log:
            self.api.output('Connection Response: %s %s' % (self, data))
        if self.response_pending:
            self.response_rows.append(data['row'])
            if data['complete']:
                if self.response_callback:
                    self.response_callback.complete(self.response_rows)
                    self.response_callback = None
                self.response_pending = None
                self.response_rows = None
        else:
            self.api.error_handler(id, 'Response Unexpected: %s' % data)

    def handle_response_failure(self):
        if self.response_callback:
            self.response_callback.complete(None)

    def handle_status(self, data):
        if self.log:
            self.api.output('Connection Status: %s %s' % (self, data))
        if self.status_pending and data['msg'] == self.status_pending:
            # if update_handler is set (an Advise is active) then leave status_pending, because we'll 
            # get sporadic OnOtherAck status messages mixed in with the update messages
            # in all other cases, clear status_pending, since we only expect the one status message
            if not self.update_handler:
                self.status_pending = None

            if data['status'] == '1':
                # special case for the first status ack of a new connection; we may need to do on_connect_action
                if data['msg'] == 'OnInitAck':
                    self.connected = True
                    if self.on_connect_action:
                        self.ready = True
                        cmd, arg, exa, cba, exr, cbr, exs, cbs, cbu, uhr = self.on_connect_action
                        self.api.output('%s sending on_connect_action: %s' % (repr(self), repr(self.on_connect_action)))
                        self.send(cmd, arg, exa, cba, exr, cbr, exs, cbs, cbu, uhr)
                        self.on_connect_action = None
                        print('after on_connect_action send: self.status_pending=%s' % self.status_pending)

                if self.status_callback:
                    self.status_callback.complete(data)
                    self.status_callback = None
            else:
                self.api.error_handler(self.id, 'Status Error: %s' % data)
        else:
            self.api.error_handler(self.id, 'Status Unexpected: %s' % data)
            # if ADVISE is active; call handler function with None to notifiy caller the advise has been terminated
            if self.update_handler and data['msg']=='OnTerminate':
                self.update_handler(self, None)
            self.handle_response_failure()

    def handle_update(self, data):
        if self.log:
            self.api.output('Connection Update: %s %s' % (self, repr(d)))
        if self.update_callback:
            self.update_callback.complete(data['row'])
            self.update_callback = None
        else:
            if self.update_handler:
                self.update_handler(self, data['row'])
            else:
                self.api.error_handler(self.id, 'Update Unexpected: %s' % repr(data))

    def query(self, cmd, table, what, where, ex_ack=None, cb_ack=None, ex_response=None, cb_response=None, ex_status=None, cb_status=None, cb_update=None, update_handler=None):
        tql='%s;%s;%s' % (table, what, where)
        self.last_query='%s: %s' % (cmd, tql)
        ret = self.send(cmd, tql, ex_ack, cb_ack, ex_response, cb_response, ex_status, cb_status, cb_update, update_handler)

    def request(self, table, what, where, callback):
        return self.query('request', table, what, where, 'REQUEST_OK', None, True, callback)

    def advise(self, table, what, where, handler):
        return self.query('advise', table, what, where, 'ADVISE_OK', None, None, None, 'OnOtherAck', None, None, handler)

    def adviserequest(self, table, what, where, callback, handler):
        return self.query('adviserequest', table, what, where, 'ADVISEREQUEST_OK', None, True, callback, 'OnOtherAck', None, None, handler)

    def unadvise(self, table, what, where, callback):
        return self.query('unadvise', table, what, where, 'UNADVISE_OK', None, None, None, 'OnOtherAck', callback)

    def poke(self, table, what, where, data, ack_callback, callback):
        tql = '%s;%s;%s!%s' % (table, what, where, data)
        self.last_query = 'poke: %s' % tql
        return self.send('poke', tql, "POKE_OK", ack_callback, None, None, 'OnOtherAck', callback)

    def execute(self, command, callback):
        self.last_query = 'execute: %s' % command
        return self.send('execute', command, "EXECUTE_OK", callback)

    def terminate(self, code, callback):
        self.last_query = 'terminate: %s' % str(code) 
        return self.send('terminate', str(code), "TERMINATE_OK", callback)

    def send(self, cmd, args, ex_ack=None, cb_ack=None, ex_response=None, cb_response=None, ex_status=None, cb_status=None, cb_update=None, update_handler=None):
        if self.ready:
            self.cmd = cmd
            if 'request' in cmd:
                self.response_rows = []
            ret = self.api.gateway_send('%s %s %s' % (cmd, self.id, args))
            self.ack_pending = ex_ack
            self.ack_callback = cb_ack
            self.response_pending = ex_response
            self.response_callback = cb_response
            self.status_pending = ex_status
            self.status_callback = cb_status
            self.update_callback = cb_update
            self.update_handler = update_handler
        else:
            if self.on_connect_action:
                self.api.error_handler(self.id, 'Failure: on_connect_action already exists: %s' % repr(self.on_connect_action))
                ret = False
            else:
                self.api.output('%s storing on_connect_action (%s)...' % (self, cmd))
                self.on_connect_action = (cmd, args, ex_ack, cb_ack, ex_response, cb_response, ex_status, cb_status, cb_update, update_handler)
                ret = True
        return ret


class RTX_LocalCallback(object):
    def __init__(self, api, callback_handler, errback_handler=None):
        self.api = api
        self.callable = callback_handler
        self.errback_handler = errback_handler

    def callback(self, data):
        if self.callable:
            self.callable(data)
        else:
            self.api.error_handler(repr(self), 'Failure: undefined callback_handler for Connection: %s data=%s' % (repr(self), repr(data)))

    def errback(self, error):
        if self.errback_handler:
            self.errback_handler(error)
        else:
            self.api.error_handler(repr(self), 'Failure: undefined errback_handler for Connection: %s error=%s' % (repr(self), repr(error)))


class RTX(object):
    def __init__(self):
        self.label = 'RTX Gateway'
        self.channel = 'rtx'
        self.id = 'RTX'
        self.output('RTX init')
        self.config = Config(self.channel)
        self.api_hostname = self.config.get('API_HOST')
        self.api_port = int(self.config.get('API_PORT'))
        self.username = self.config.get('USERNAME')
        self.password = self.config.get('PASSWORD')
        self.http_port = int(self.config.get('HTTP_PORT'))
        self.tcp_port = int(self.config.get('TCP_PORT'))
        self.enable_ticker = bool(int(self.config.get('ENABLE_TICKER')))
        self.enable_high_low= bool(int(self.config.get('ENABLE_HIGH_LOW')))
        self.enable_seconds_tick = bool(int(self.config.get('ENABLE_SECONDS_TICK')))
        self.log_api_messages = bool(int(self.config.get('LOG_API_MESSAGES')))
        self.debug_api_messages = bool(int(self.config.get('DEBUG_API_MESSAGES')))
        self.log_client_messages = bool(int(self.config.get('LOG_CLIENT_MESSAGES')))
        self.log_order_updates = bool(int(self.config.get('LOG_ORDER_UPDATES')))
        self.callback_timeout = {}
        for t in TIMEOUT_TYPES:
            self.callback_timeout[t] = int(self.config.get('TIMEOUT_%s' % t))
            self.output('callback_timeout[%s] = %d' % (t, self.callback_timeout[t]))
        self.now = None
        self.feedzone = pytz.timezone(self.config.get('API_TIMEZONE'))
        self.localzone = tzlocal.get_localzone()
        self.current_account = ''
        self.clients = set([])
        self.orders = {}
        self.pending_orders = {}
        self.tickets = {}
        self.pending_tickets = {}
        self.openorder_callbacks = []
        self.accounts = None
        self.account_data = {}
        self.pending_account_data_requests = set([])
        self.positions = {}
        self.position_callbacks = []
        self.executions = {}
        self.execution_callbacks = []
        self.order_callbacks = []
        self.bardata_callbacks = []
        self.cancel_callbacks = []
        self.order_status_callbacks = []
        self.ticket_callbacks = []
        self.add_symbol_callbacks = []
        self.accountdata_callbacks = []
        self.set_account_callbacks = []
        self.account_request_callbacks = []
        self.account_request_pending = True
        self.timer_callbacks = []
        self.connected = False
        self.last_connection_status = ''
        self.connection_status = 'Initializing'
        self.LastError = -1
        self.next_order_id = -1
        self.last_minute = -1
        self.symbols = {}
        self.primary_exchange_map = {}
        self.gateway_sender = None
        self.active_cxn = {}
        self.idle_cxn = {}
        self.cx_time = None
        self.seconds_disconnected = 0
        self.callback_metrics = {}
        self.set_order_route(self.config.get('API_ROUTE'), None)
        reactor.connectTCP(self.api_hostname, self.api_port, RtxClientFactory(self))
        self.repeater = LoopingCall(self.EverySecond)
        self.repeater.start(1)

    def record_callback_metrics(self, label, elapsed, expired):
        m = self.callback_metrics.setdefault(label, {'tot':0, 'min': 9999, 'max': 0, 'avg': 0, 'exp': 0, 'hst': []})
        total = m['tot']  
        m['tot'] += 1
        m['min'] = min(m['min'], elapsed)
        m['max'] = max(m['max'], elapsed)
        m['avg'] = (m['avg'] * total + elapsed) / (total + 1)
        m['exp'] += int(expired)
        m['hst'].append(elapsed)
        if len(m['hst']) > CALLBACK_METRIC_HISTORY_LIMIT:
          del m['hst'][0]

        
    def cxn_register(self, cxn):
        if ENABLE_CXN_DEBUG:
            self.output('cxn_register: %s' % repr(cxn))
        self.active_cxn[cxn.id] = cxn

    def cxn_activate(self, cxn):
        if ENABLE_CXN_DEBUG:
            self.output('cxn_activate: %s' % repr(cxn))
        if not cxn.key in self.idle_cxn.keys():
            self.idle_cxn[cxn.key] = []
        self.idle_cxn[cxn.key].append(cxn)

    def cxn_get(self, service, topic):
        key = '%s;%s' % (service, topic)
        if key in self.idle_cxn.keys() and len(self.idle_cxn[key]):
            cxn = self.idle_cxn[key].pop()
        else:
            cxn = RTX_Connection(self, service, topic)
        if ENABLE_CXN_DEBUG:
            self.output('cxn_get() returning: %s' % repr(cxn))
        return cxn

    def gateway_connect(self, protocol):
        if protocol:
            self.gateway_sender = protocol.sendLine
            self.gateway_transport = protocol.transport
            self.update_connection_status('Connecting')
        else:
            self.gateway_sender = None
            self.connected = False
            self.seconds_disconnected = 0
            self.account_request_pending = False
            self.accounts = None
            self.update_connection_status('Disconnected')
            self.error_handler(self.id, 'error: API Disconnected')

        return self.gateway_receive

    def gateway_send(self, msg):
        if self.debug_api_messages:
            self.output('<--TX[%d]--' % (len(msg)))
            hexdump(msg)
        if self.log_api_messages:
            self.output('<-- %s' % repr(msg))
        if self.gateway_sender:
            self.gateway_sender('%s\n' % str(msg))


    def dump_input_message(self, msg):
        self.output('--RX[%d]-->' % (len(msg)))
        hexdump(msg)

    def receive_exception(self, t, e, msg):
        self.error_handler(self.id, 'Exception %s %s parsing data from RTGW' % (t, e))
        self.dump_input_message(msg)
        return None

    def gateway_receive(self, msg):
        """handle input from rtgw """

        if self.debug_api_messages:
            self.dump_input_message(msg)

        try:
            o = json.loads(msg)
        except Exception as e:
            return self.receive_exception(sys.exc_info()[0], e, msg)

        msg_type = o['type']
        msg_id = o['id']
        msg_data = o['data']

        if self.log_api_messages:
            self.output('--> %s %s %s' % (msg_type, msg_id, msg_data))

        if msg_type == 'system':
            self.handle_system_message(msg_id, msg_data)
        else:
            if msg_id in self.active_cxn.keys():
                c = self.active_cxn[msg_id].receive(msg_type, msg_data)
            else:
                self.error_handler(self.id, 'Message Received on Unknown connection: %s' % repr(msg))

        return True

    def handle_system_message(self, id, data):
        if data['msg'] == 'startup':
            self.connected = True
            self.accounts = None
            self.update_connection_status('Startup')
            self.output('Connected to %s' % data['item'])
            self.setup_local_queries()
        else:
            self.error_handler(self.id, 'Unknown system message: %s' % repr(data))

    def setup_local_queries(self):
        """Upon connection to rtgw, start automatic queries"""
        #what='BANK,BRANCH,CUSTOMER,DEPOSIT'
        what='*'
        self.rtx_request('ACCOUNT_GATEWAY', 'ORDER', 'ACCOUNT', what, '',
                         'accounts', self.handle_accounts, self.accountdata_callbacks, self.callback_timeout['ACCOUNT'],
                         self.handle_initial_account_failure)

        self.cxn_get('ACCOUNT_GATEWAY', 'ORDER').advise('ORDERS', '*', '', self.handle_order_update)
        
        self.rtx_request('ACCOUNT_GATEWAY', 'ORDER', 'ORDERS', '*', '',
                        'orders', self.handle_initial_orders_response, self.openorder_callbacks, self.callback_timeout['ORDERSTATUS'])

    def handle_initial_account_failure(self, message):
        self.force_disconnect('Initial account query failed (%s)' % repr(message))

    def handle_initial_orders_response(self, rows):
        self.output('Initial Orders refresh complete.')

    def output(self, msg):
        if 'error' in msg:
            log.err(msg)
        else:
            log.msg(msg)

    def open_client(self, client):
        self.clients.add(client)

    def close_client(self, client):
        self.clients.discard(client)
        symbols = self.symbols.values()
        for ts in symbols:
            if client in ts.clients:
                ts.del_client(client)
                if not ts.clients:
                    del(self.symbols[ts.symbol])

    def set_primary_exchange(self, symbol, exchange):
        if exchange:
            self.primary_exchange_map[symbol] = exchange
        else:
            del(self.primary_exchange_map[symbol])
        return self.primary_exchange_map

    def CheckPendingResults(self):
        # check each callback list for timeouts
        for cblist in [self.timer_callbacks, self.position_callbacks, self.ticket_callbacks, self.openorder_callbacks, self.execution_callbacks, self.bardata_callbacks, self.order_callbacks, self.cancel_callbacks, self.add_symbol_callbacks, self.accountdata_callbacks, self.set_account_callbacks, self.account_request_callbacks, self.order_status_callbacks]:
            dlist = []
            for cb in cblist:
                cb.check_expire()
                if cb.done:
                    dlist.append(cb)
            # delete any callbacks that are done
            for cb in dlist:
                cblist.remove(cb)

    def handle_order_update(self, cxn, msg):
        if msg:
          self.handle_order_response(msg)
        else:
          self.force_disconnect('API Order Status ADVISE connection has been terminated; connection has failed')

    def handle_order_response(self, msg):
        #print('---handle_order_response: %s' % repr(msg))
        oid = msg['ORIGINAL_ORDER_ID'] if 'ORIGINAL_ORDER_ID' in msg else None
        ret = None
        if oid:
            if self.pending_orders and 'CLIENT_ORDER_ID' in msg:
                # this is a newly created order, it has a CLIENT_ORDER_ID
                coid = msg['CLIENT_ORDER_ID']
                if coid in self.pending_orders:
                    self.pending_orders[coid].initial_update(msg)
                    self.orders[oid] = self.pending_orders[coid]
                    del self.pending_orders[coid]
            elif self.pending_orders and (oid in self.pending_orders.keys()):
                # this is a change order, ORIGINAL_ORDER_ID will be a key in pending_orders
                self.pending_orders[oid].initial_update(msg)
                del self.pending_orders[oid]
            elif oid in self.orders.keys():
                # this is an existing order, so update it
                self.orders[oid].update(msg)
            else:
                # we've never seen this order, so add it to the collection and update it
                o = API_Order(self, oid, {})
                self.orders[oid]=o
                o.update(msg)
        else:
            self.error_handler(self.id, 'handle_order_update: ORIGINAL_ORDER_ID not found in %s' % repr(msg))
            #self.output('error: handle_order_update: ORIGINAL_ORDER_ID not found in %s' % repr(msg))

    def handle_ticket_update(self, cxn, msg):
        return self.handle_ticket_response(msg)

    def handle_ticket_response(self, msg):
        tid = msg['CLIENT_ORDER_ID'] if 'CLIENT_ORDER_ID' in msg else None
        if self.pending_tickets and tid in self.pending_tickets.keys():
            self.pending_tickets[tid].initial_update(msg)
            self.tickets[tid] = self.pending_tickets[tid]
            del self.pending_tickets[tid]

    def send_order_status(self, order):
        fields = order.render()
        self.WriteAllClients('order.%s %s %s %s' % (fields['permid'], fields['account'], fields['TYPE'], fields['status']))

    def make_account(self, row):
        return '%s.%s.%s.%s' % (row['BANK'], row['BRANCH'], row['CUSTOMER'], row['DEPOSIT'])

    def handle_accounts(self, rows):
        rows = json.loads(rows)
        if rows:
            self.accounts = list(set([self.make_account(row) for row in rows]))
            self.accounts.sort()
            self.account_request_pending = False
            self.WriteAllClients('accounts: %s' % json.dumps(self.accounts))
            self.update_connection_status('Up')
            for cb in self.account_request_callbacks:
                cb.complete(self.accounts)

            for cb in self.set_account_callbacks:
                self.output('set_account: processing deferred response.')
                self.process_set_account(cb.id, cb)
        else:
            self.handle_initial_account_failure('initial account query returned no data')

    def set_account(self, account_name, callback):
        cb = API_Callback(self, account_name, 'set-account', callback)
        if self.accounts:
            self.process_set_account(account_name, cb)
        elif self.account_request_pending:
            self.set_account_callbacks.append(cb)
        else:
            self.error_handler(self.id, 'set_account; no data, but no account_request_pending')
            cb.complete(None)

    def verify_account(self, account_name):
        if account_name in self.accounts:
            ret = True
        else:
            msg = 'account %s not found' % account_name
            self.error_handler(self.id, 'set_account(): %s' % msg)
            ret = False
        return ret

    def process_set_account(self, account_name, callback):
        ret = self.verify_account(account_name)
        if ret:
            self.current_account = account_name
            self.WriteAllClients('current-account: %s' % self.current_account)

        if callback:
            callback.complete(ret)
        else:
            return ret

    def rtx_request(self, service, topic, table, what, where, label, handler, cb_list, timeout, error_handler=None):
        cxn = self.cxn_get(service, topic)
        cb = API_Callback(self, cxn.id, label, RTX_LocalCallback(self, handler, error_handler), timeout)
        cxn.request(table, what, where, cb)
        cb_list.append(cb)

    def EverySecond(self):
        if self.connected:
            if self.enable_seconds_tick:
                self.rtx_request('TA_SRV', 'LIVEQUOTE', 'LIVEQUOTE', 'DISP_NAME,TRDTIM_1,TRD_DATE',
                                 "DISP_NAME='$TIME'", 'tick', self.handle_time, self.timer_callbacks, 
                                 self.callback_timeout['TIMER'], self.handle_time_error)
        else:
            self.seconds_disconnected += 1
            if self.seconds_disconnected > DISCONNECT_SECONDS:
                if SHUTDOWN_ON_DISCONNECT:
                    self.force_disconnect('Realtick Gateway connection timed out after %d seconds' % self.seconds_disconnected)
        self.CheckPendingResults()

        if not int(time.time()) % 60:
            self.EveryMinute()

    def EveryMinute(self):
        if self.callback_metrics:
            self.output('callback_metrics: %s' % json.dumps(self.callback_metrics))   

    def WriteAllClients(self, msg):
        if self.log_client_messages:
            self.output('WriteAllClients: %s.%s' % (self.channel, msg))
        msg = str('%s.%s' % (self.channel, msg))
        for c in self.clients:
            c.sendString(msg)

    def error_handler(self, id, msg):
        """report error messages"""
        self.output('ALERT: %s %s' % (id, msg))
        self.WriteAllClients('error: %s %s' % (id, msg))

    def force_disconnect(self, reason):
        self.update_connection_status('Disconnected')
        self.error_handler(self.id, 'API Disconnect: %s' % reason)
        reactor.stop()

    def parse_tql_float(self, data, pid, label):
        ret = self.parse_tql_field(data, pid, label)
        return round(float(ret),2) if ret else 0.0

    def parse_tql_int(self, data, pid, label):
        ret = self.parse_tql_field(data, pid, label)
        return int(ret) if ret else 0

    def parse_tql_str(self, data, pid, label):
        ret = self.parse_tql_field(data, pid, label)
        return str(ret) if ret else ''

    def parse_tql_field(self, data, pid, label):
        if data.lower().startswith('error '):
            if data.lower()=='error 0':
                code = 'Field Not Found'
            elif data.lower() == 'error 2':
                code = 'Field No Value'
            elif data.lower() == 'error 3':
                code = 'Field Not Permissioned'
            elif data.lower() == 'error 17':
                code = 'No Record Exists'
            elif data.lower() == 'error 256':
                code = 'Field Reset'
            else:
                code = 'Unknown Field Error'
            self.error_handler(pid, 'Field Parse Failure: %s=%s (%s)' % (label, repr(data), code))
            ret = None
        else:
            ret = data
        return ret

    def handle_time(self, rows):
        rows = json.loads(rows)
        if rows:
            time_field = rows[0]['TRDTIM_1']
            date_field = rows[0]['TRD_DATE']
            if time_field == 'Error 17':
                # this indicates the $TIME symbol is not found on the server, which is a kludge to determine the login has failed
                self.force_disconnect('Gateway reports $TIME symbol unknown; connection has failed')
            
            elif time_field.lower().startswith('error'):
                self.error_handler(self.id, 'handle_time: time field %s' % time_field)
            else:
                year, month, day = [int(i) for i in date_field.split('-')[0:3]]
                hour, minute, second = [int(i) for i in time_field.split(':')[0:3]]
                self.now = self.feedzone.localize(datetime.datetime(year,month,day,hour,minute,second)).astimezone(self.localzone)
                if minute != self.last_minute:
                    self.last_minute = minute
                    self.WriteAllClients('time: %s %s:00' % (self.now.strftime('%Y-%m-%d'), self.now.strftime('%H:%M')))
        else:
            self.error_handler(self.id, 'handle_time: unexpected null input')

    def handle_time_error(self, error):
        #time timeout error is reported as an expired callback
        self.output('time_error: %s' % repr(error))

    def connect(self):
        self.update_connection_status('Connecting')
        self.output('Awaiting startup response from RTX gateway at %s:%d...' % (self.api_hostname, self.api_port))

    def market_order(self, account, route, symbol, quantity, callback):
        return self.submit_order(account, route, 'market', 0, 0, symbol, int(quantity), callback)

    def limit_order(self, account, route, symbol, limit_price, quantity, callback):
        return self.submit_order(account, route, 'limit', float(limit_price), 0, symbol, int(quantity), callback)

    def stop_order(self, account, route, symbol, stop_price, quantity, callback):
        return self.submit_order(account, route, 'stop', 0, float(stop_price), symbol, int(quantity), callback)

    def stoplimit_order(self, account, route, symbol, stop_price, limit_price, quantity, callback):
        return self.submit_order(account, route, 'stoplimit', float(limit_price), float(stop_price), symbol, int(quantity), callback)

    def stage_market_order(self, tag, account, route, symbol, quantity, callback):
        return self.submit_order(account, route, 'market', 0, 0, symbol, int(quantity), callback, staged=tag)

    def create_order_id(self):
        return str(uuid1())

    def create_staged_order_ticket(self, account, callback):

        if not self.verify_account(account):
          API_Callback(self, 0, 'create-staged-order-ticket', callback).complete({'status': 'Error', 'errorMsg': 'account unknown'})
          return

        o=OrderedDict({})
        self.verify_account(account)
        bank, branch, customer, deposit = account.split('.')[:4]
        o['BANK']=bank
        o['BRANCH']=branch
        o['CUSTOMER']=customer
        o['DEPOSIT']=deposit
        tid = 'T-%s' % self.create_order_id() 
        o['CLIENT_ORDER_ID']=tid
        o['DISP_NAME']='N/A'
        o['STYP']=RTX_STYPE # stock
        o['EXIT_VEHICLE']='NONE'
        o['TYPE']='UserSubmitStagedOrder'

        # create callback to return to client after initial order update
        cb = API_Callback(self, tid, 'ticket', callback, self.callback_timeout['ORDER'])
        self.ticket_callbacks.append(cb)
        self.pending_tickets[tid]=API_Order(self, tid, o, cb)
        fields= ','.join(['%s=%s' %(i,v) for i,v in o.iteritems()])

        acb = API_Callback(self, tid, 'ticket-ack', RTX_LocalCallback(self, self.ticket_submit_ack_callback), self.callback_timeout['ORDER'])
        cb = API_Callback(self, tid, 'ticket', RTX_LocalCallback(self, self.ticket_submit_callback), self.callback_timeout['ORDER'])
        self.cxn_get('ACCOUNT_GATEWAY', 'ORDER').poke('ORDERS', '*', '', fields, acb, cb)

    def ticket_submit_ack_callback(self, data):
        """called when staged order ticket request has been submitted with 'poke' and Ack has returned""" 
        self.output('staged order ticket submission acknowledged: %s' % repr(data))

    def ticket_submit_callback(self, data):
        """called when staged order ticket request has been submitted with 'poke' and OnOtherAck has returned""" 
        self.output('staged order ticket submitted: %s' % repr(data))

    def submit_order(self, account, route, order_type, price, stop_price, symbol, quantity, callback, staged=None, oid=None):

        if not self.verify_account(account):
          API_Callback(self, 0, 'submit-order', callback).complete({'status': 'Error', 'errorMsg': 'account unknown'})
          return
        #bank, branch, customer, deposit = self.current_account.split('.')[:4]
        self.set_order_route(route, None)
        if type(self.order_route) != dict:
          API_Callback(self, 0, 'submit-order', callback).complete({'status': 'Error', 'errorMsg': 'undefined order route: %s' % repr(self.order_route)})
          return

        o=OrderedDict({})
        bank, branch, customer, deposit = account.split('.')[:4]
        o['BANK']=bank
        o['BRANCH']=branch
        o['CUSTOMER']=customer
        o['DEPOSIT']=deposit

        o['BUYORSELL']='Buy' if quantity > 0 else 'Sell' # Buy Sell SellShort
        o['GOOD_UNTIL']='DAY' # DAY or YYMMDDHHMMSS
        route = self.order_route.keys()[0]
        o['EXIT_VEHICLE']=route
        
        # if order_route has a value, it is a dict of order route parameters
        if self.order_route[route]:
            for k,v in self.order_route[route].items():
                # encode strategy parameters in 0x01 delimited format
                if k in ['STRAT_PARAMETERS', 'STRAT_REDUNDANT_DATA']:
                    v = ''.join(['%s\x1F%s\x01' % i for i in v.items()])
                o[k]=v

        o['DISP_NAME']=symbol
        o['STYP']=RTX_STYPE # stock

        if symbol in self.primary_exchange_map.keys():
            exchange = self.primary_exchange_map[symbol]
        else:
            exchange = RTX_EXCHANGE
        o['EXCHANGE']=exchange
        
        if order_type == 'market':
            o['PRICE_TYPE'] = 'Market'
        elif order_type=='limit':
            o['PRICE_TYPE']='AsEntered' 
            o['PRICE']=price
        elif order_type=='stop':
            o['PRICE_TYPE']='Stop' 
            o['STOP_PRICE']=stop_price
        elif type=='stoplimit':
            o['PRICE_TYPE']='StopLimit' 
            o['STOP_PRICE']=stop_price
            o['PRICE']=price
        else:
            msg = 'unknown order type: %s' % order_type
            self.error_handler(self.id, msg)
            raise Exception(msg)

        o['VOLUME_TYPE']='AsEntered'
        o['VOLUME']=abs(quantity)
        
        if staged:
            o['ORDER_TAG'] = staged
            staging = 'Staged'
        else:
            staging = ''

        if oid:
            o['REFERS_TO_ID'] = oid
            submission = 'Change'
        else:
            oid = self.create_order_id()
            o['CLIENT_ORDER_ID']=oid
            submission = 'Order'
            
        o['TYPE']='UserSubmit%s%s' % (staging, submission)

        # create callback to return to client after initial order update
        cb = API_Callback(self, oid, 'order', callback, self.callback_timeout['ORDER'])
        self.order_callbacks.append(cb)
        if oid in self.orders:
            self.pending_orders[oid]=self.orders[oid]
            self.orders[oid].callback = cb
        else:
            self.pending_orders[oid]=API_Order(self, oid, o, cb)

        fields= ','.join(['%s=%s' %(i,v) for i,v in o.iteritems()])

        acb = API_Callback(self, oid, 'order-ack', RTX_LocalCallback(self, self.order_submit_ack_callback), self.callback_timeout['ORDER'])
        cb = API_Callback(self, oid, 'order', RTX_LocalCallback(self, self.order_submit_callback), self.callback_timeout['ORDER'])
        self.cxn_get('ACCOUNT_GATEWAY', 'ORDER').poke('ORDERS', '*', '', fields, acb, cb)

    def order_submit_ack_callback(self, data):
        """called when order has been submitted with 'poke' and Ack has returned""" 
        self.output('order submission acknowleded: %s' % repr(data))

    def order_submit_callback(self, data):
        """called when order has been submitted with 'poke' and OnOtherAck has returned""" 
        self.output('order submitted: %s' % repr(data))

    def cancel_order(self, oid, callback):
        self.output('cancel_order %s' % oid)
        cb = API_Callback(self, oid, 'cancel_order', callback, self.callback_timeout['ORDER'])
        order = self.orders[oid] if oid in self.orders else None
        if order:
            if order.fields['status'] == 'Canceled':
                cb.complete({'status': 'Error', 'errorMsg': 'Already canceled.', 'id': oid})
            else:
                msg=OrderedDict({})
                #for fid in ['DISP_NAME', 'STYP', 'ORDER_TAG', 'EXIT_VEHICLE']:
                #    if fid in order.fields:
                #        msg[fid] = order.fields[fid]
                msg['TYPE']='UserSubmitCancel'
                msg['REFERS_TO_ID']=oid
                fields= ','.join(['%s=%s' %(i,v) for i,v in msg.iteritems()])
                self.cxn_get('ACCOUNT_GATEWAY', 'ORDER').poke('ORDERS', '*', '', fields, None, cb)
                self.cancel_callbacks.append(cb)
        else:
            cb.complete({'status': 'Error', 'errorMsg': 'Order not found', 'id': oid})

    def symbol_enable(self, symbol, client, callback):
        self.output('symbol_enable(%s,%s,%s)' % (symbol, client, callback))
        if not symbol in self.symbols.keys():
            cb = API_Callback(self, symbol, 'add-symbol', callback, self.callback_timeout['ADDSYMBOL'])
            symbol = API_Symbol(self, symbol, client, cb)
            self.add_symbol_callbacks.append(cb)
        else:
            self.symbols[symbol].add_client(client)
            API_Callback(self, symbol, 'add-symbol', callback).complete(True)
        self.output('symbol_enable: symbols=%s' % repr(self.symbols))

    def symbol_init(self, symbol):
        ret = not 'SYMBOL_ERROR' in symbol.rawdata.keys()
        if not ret:
            self.symbol_disable(symbol.symbol, list(symbol.clients)[0])
        symbol.callback.complete(ret)
        return ret

    def symbol_disable(self, symbol, client):
        self.output('symbol_disable(%s,%s)' % (symbol, client))
        self.output('self.symbols=%s' % repr(self.symbols))
        if symbol in self.symbols.keys():
            ts = self.symbols[symbol]
            ts.del_client(client)
            if not ts.clients:
                del(self.symbols[symbol])
            self.output('ret True: self.symbols=%s' % repr(self.symbols))
            return True
        self.output('ret False: self.symbols=%s' % repr(self.symbols))

    def update_connection_status(self, status):
        self.connection_status = status
        if status != self.last_connection_status:
            self.last_connection_status = status
            self.WriteAllClients('connection-status-changed: %s' % status)

    def request_accounts(self, callback):
        cb = API_Callback(self, 0, 'request-accounts', callback, self.callback_timeout['ACCOUNT'])
        if self.accounts:
            cb.complete(self.accounts)
        elif self.account_request_pending:
            self.account_request_callbacks.append(cb)
        else:
            self.output(
                'Error: request_accounts; no data, but no account_request_pending')
            cb.complete(None)

    def request_positions(self, callback):
        cxn = self.cxn_get('ACCOUNT_GATEWAY', 'ORDER')
        cb = API_Callback(self, 0, 'positions', callback, self.callback_timeout['POSITION'])
        cxn.request('POSITION', '*', '', cb)
        self.position_callbacks.append(cb)

    def request_orders(self, callback):
        cxn = self.cxn_get('ACCOUNT_GATEWAY', 'ORDER')
        cb = API_Callback(self, 0, 'orders', callback, self.callback_timeout['ORDERSTATUS'])
        cxn.request('ORDERS', '*', '', cb)
        self.openorder_callbacks.append(cb)

    def request_order(self, oid, callback):
        cb = API_Callback(self, oid, 'order_status', callback, self.callback_timeout['ORDERSTATUS'])
        self.cxn_get('ACCOUNT_GATEWAY', 'ORDER').request('ORDERS', '*', "ORIGINAL_ORDER_ID='%s'" % oid, cb)
        self.order_status_callbacks.append(cb)

    def request_executions(self, callback):
        cb = API_Callback(self, 0, 'executions', callback, self.callback_timeout['ORDERSTATUS'])
        self.cxn_get('ACCOUNT_GATEWAY', 'ORDER').request('ORDERS', '*', '', cb)
        self.execution_callbacks.append(cb)

    def request_account_data(self, account, fields, callback):
        cxn = self.cxn_get('ACCOUNT_GATEWAY', 'ORDER')
        cb = API_Callback(self, 0, 'account_data', callback, self.callback_timeout['ACCOUNT'])
        bank, branch, customer, deposit = account.split('.')[:4]
        tql_where = "BANK='%s',BRANCH='%s',CUSTOMER='%s',DEPOSIT='%s'" % (bank,branch,customer,deposit)
        if fields:
            fields = ','.join(fields)
        else:
            fields = '*'
        cxn.request('DEPOSIT', fields, tql_where, cb)
        self.accountdata_callbacks.append(cb)

    def request_global_cancel(self):
        self.rtx_request('ACCOUNT_GATEWAY', 'ORDER', 
                        'ORDERS', 'ORDER_ID,ORIGINAL_ORDER_ID,CURRENT_STATUS,TYPE', "CURRENT_STATUS={'LIVE','PENDING'}",
                        'global_cancel', self.handle_global_cancel, self.openorder_callbacks, self.callback_timeout['ORDER'])

    def handle_global_cancel(self, rows):
        rows = json.loads(rows)
        for row in rows:
            if row['CURRENT_STATUS'] in ['LIVE', 'PENDING']:
                self.cancel_order(row['ORIGINAL_ORDER_ID'], RTX_LocalCallback(self, self.global_cancel_callback))

    def global_cancel_callback(self, data):
        data = json.loads(data)
        self.output('global cancel: %s' % repr(data))

    def query_bars(self, symbol, period, bar_start, bar_end, callback):
        self.error_handler(self.id, 'ALERT: query_bars unimplemented')
        return None

    def handle_historical_data(self, msg):
        for cb in self.bardata_callbacks:
            if cb.id == msg.reqId:
                if not cb.data:
                    cb.data = []
                if msg.date.startswith('finished'):
                    cb.complete(['OK', cb.data])
                else:
                    cb.data.append(dict(msg.items()))
        # self.output('historical_data: %s' % msg) #repr((id, start_date, bar_open, bar_high, bar_low, bar_close, bar_volume, count, WAP, hasGaps)))

    def query_connection_status(self):
        return self.connection_status

    def set_order_route(self, route, callback):
        #print('set_order_route(%s, %s) type=%s %s' % (repr(route), repr(callback), type(route), (type(route) in [str, unicode])))
        if type(route) in [str, unicode]:
            if route.startswith('{'):
                route = json.loads(route)
	    elif route.startswith('"'):
                route = {json.loads(route): None}
            else:
                route = {route: None}
        if (type(route)==dict) and (len(route.keys()) == 1) and (type(route.keys()[0]) in [str, unicode]):
            self.order_route = route
            if callback:
                self.get_order_route(callback)
        else:
            if callback:
                callback.errback(Failure(Exception('cannot set order route %s' % route)))
            else:
                self.error_handler(None, 'Cannot set order route %s' % repr(route))

    def get_order_route(self, callback):
        API_Callback(self, 0, 'get_order_route', callback).complete(self.order_route)
