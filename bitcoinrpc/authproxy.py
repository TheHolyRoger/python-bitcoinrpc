
"""
  Copyright 2011 Jeff Garzik

  AuthServiceProxy has the following improvements over python-jsonrpc's
  ServiceProxy class:

  - HTTP connections persist for the life of the AuthServiceProxy object
    (if server supports HTTP/1.1)
  - sends protocol 'version', per JSON-RPC 1.1
  - sends proper, incrementing 'id'
  - sends Basic HTTP authentication headers
  - parses all JSON numbers that look like floats as Decimal
  - uses standard Python json lib

  Previous copyright, from python-jsonrpc/jsonrpc/proxy.py:

  Copyright (c) 2007 Jan-Klaas Kollhof

  This file is part of jsonrpc.

  jsonrpc is free software; you can redistribute it and/or modify
  it under the terms of the GNU Lesser General Public License as published by
  the Free Software Foundation; either version 2.1 of the License, or
  (at your option) any later version.

  This software is distributed in the hope that it will be useful,
  but WITHOUT ANY WARRANTY; without even the implied warranty of
  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
  GNU Lesser General Public License for more details.

  You should have received a copy of the GNU Lesser General Public License
  along with this software; if not, write to the Free Software
  Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA
"""

import aiohttp
from asyncio import wait_for
import base64
import decimal
import json
import logging
try:
    import urllib.parse as urlparse
except ImportError:
    import urlparse

USER_AGENT = "AuthServiceProxy/0.1"

HTTP_TIMEOUT = 30

log = logging.getLogger("BitcoinRPC")

class JSONRPCException(Exception):
    def __init__(self, rpc_error):
        parent_args = []
        try:
            parent_args.append(rpc_error['message'])
        except:
            pass
        Exception.__init__(self, *parent_args)
        self.error = rpc_error
        self.code = rpc_error['code'] if 'code' in rpc_error else None
        self.message = rpc_error['message'] if 'message' in rpc_error else None

    def __str__(self):
        return '%d: %s' % (self.code, self.message)

    def __repr__(self):
        return '<%s \'%s\'>' % (self.__class__.__name__, self)


def EncodeDecimal(o):
    if isinstance(o, decimal.Decimal):
        return float(round(o, 8))
    raise TypeError(repr(o) + " is not JSON serializable")


class JsonWrapper(object):
    def __call__(self, *args, **kwargs):
        return json.loads(*args, **kwargs, parse_float=decimal.Decimal)

class AuthServiceProxy(object):
    __id_count = 0

    def __init__(self,
                 service_url,
                 service_name=None,
                 timeout=HTTP_TIMEOUT, 
                 connection: aiohttp.ClientSession=None,
                 ssl_context=None):
        self.__service_url = service_url
        self.__service_name = service_name
        self.__url = urlparse.urlparse(service_url)
        self.__aiohttp_client_session = connection
        self.__aiohttp_must_close = False
        if self.__url.port is None:
            port = 80
        else:
            port = self.__url.port
        (user, passwd) = (self.__url.username, self.__url.password)
        try:
            user = user.encode('utf8')
        except AttributeError:
            pass
        try:
            passwd = passwd.encode('utf8')
        except AttributeError:
            pass
        authpair = user + b':' + passwd
        self.__auth_header = b'Basic ' + base64.b64encode(authpair)

        self.__timeout = timeout

    def __getattr__(self, name):
        if name.startswith('__') and name.endswith('__'):
            # Python internal stuff
            raise AttributeError
        if self.__service_name is not None:
            name = "%s.%s" % (self.__service_name, name)
        return AuthServiceProxy(self.__service_url, name, self.__timeout, self.__aiohttp_client_session)
    

    async def __call__(self, *args):
        AuthServiceProxy.__id_count += 1

        log.debug("-%s-> %s %s"%(AuthServiceProxy.__id_count, self.__service_name,
                                 json.dumps(args, default=EncodeDecimal)))
        postdata = json.dumps({'version': '1.1',
                               'method': self.__service_name,
                               'params': args,
                               'id': AuthServiceProxy.__id_count}, default=EncodeDecimal)
        request_headers = {"Host": self.__url.hostname,
                           "User-Agent": USER_AGENT,
                           "Content-type": "application/json"}
        resp = await wait_for(self._get_shared_client().request(method="POST",
                                                                url=self.__url.geturl(),
                                                                data=postdata,
                                                                headers=request_headers), self.__timeout)

        response = await self._get_response(resp)
        if self.__aiohttp_must_close:
            await self._get_shared_client().close()
        if response.get('error') is not None:
            raise JSONRPCException(response['error'])
        elif 'result' not in response:
            raise JSONRPCException({
                'code': -343, 'message': 'missing JSON-RPC result'})
        
        return response['result']

    def _get_shared_client(self) -> aiohttp.ClientSession:
        if not self.__aiohttp_client_session:
            self.__aiohttp_client_session = aiohttp.ClientSession()
            self.__aiohttp_must_close = True
        return self.__aiohttp_client_session

    async def batch_(self, rpc_calls):
        """Batch RPC call.
           Pass array of arrays: [ [ "method", params... ], ... ]
           Returns array of results.
        """
        batch_data = []
        for rpc_call in rpc_calls:
            AuthServiceProxy.__id_count += 1
            m = rpc_call.pop(0)
            batch_data.append({"jsonrpc":"2.0", "method":m, "params":rpc_call, "id":AuthServiceProxy.__id_count})

        postdata = json.dumps(batch_data, default=EncodeDecimal)
        log.debug("--> "+postdata)
        request_headers = {"Host": self.__url.hostname,
                           "User-Agent": USER_AGENT,
                           "Authorization": self.__auth_header,
                           "Content-type": "application/json"}
        resp = await self._get_shared_client().request(method="POST",
                                                       url=self.__url.geturl(),
                                                       data=postdata,
                                                       headers=request_headers)
        results = []
        responses = await self._get_response(resp)
        if isinstance(responses, (dict,)):
            if ('error' in responses) and (responses['error'] is not None):
                raise JSONRPCException(responses['error'])
            raise JSONRPCException({
                'code': -32700, 'message': 'Parse error'})
        for response in responses:
            if response['error'] is not None:
                raise JSONRPCException(response['error'])
            elif 'result' not in response:
                raise JSONRPCException({
                    'code': -343, 'message': 'missing JSON-RPC result'})
            else:
                results.append(response['result'])
        return results

    async def _get_response(self, resp):
        try:
            json_response = await resp.json(loads=JsonWrapper())
        except:
            json_response = None
        if self.__aiohttp_must_close:
            await self._get_shared_client().close()
        if json_response is None:
            raise JSONRPCException({
                'code': -342, 'message': 'missing HTTP response from server'})

        content_type = resp.headers.get('Content-Type')
        if content_type != 'application/json':
            raise JSONRPCException({
                'code': -342, 'message': 'non-JSON HTTP response with \'%i %s\' from server' % (resp.status, resp.reason)})

        if "error" in json_response and json_response["error"] is None:
            log.debug("<-%s- %s"%(json_response["id"], json.dumps(json_response["result"], default=EncodeDecimal)))
        else:
            log.debug("<-- "+str(resp))
        return json_response
