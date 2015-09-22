# Copyright (c) 2015 OpenStack Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
The simple scheme used here for testing the encryption feature in swift is as
follows: every path is associated with a key, where the key is derived from the
path itself in a deterministic fashion such that the key does not need to be
stored. Specifically, the key for any path is an HMAC of a root key and the
path itself, calculated using an SHA256 hash function::

  <path_key> = HMAC_SHA256(<root_key>, <path>)
"""

import base64
import hashlib
import hmac
import os

from swift.common.utils import get_logger, split_path
from swift.common.request_helpers import get_obj_persisted_sysmeta_prefix, \
    is_sys_meta, strip_sys_meta_prefix
from swift.common.wsgi import WSGIContext
from swift.common.swob import Request, HTTPException, HTTPUnprocessableEntity


class TrivialKeyMasterContext(WSGIContext):
    def __init__(self, keymaster, account, container, obj):
        super(TrivialKeyMasterContext, self).__init__(keymaster.app)
        self.keymaster = keymaster
        self.logger = keymaster.logger
        self.account = account
        self.container = container
        self.obj = obj
        self._init_keys()

    def _init_keys(self):
        """
        Setup default container and object keys based on the request path.
        """
        self.keys = {}
        self.account_path = os.path.join(os.sep, self.account)
        self.container_path = self.obj_path = None
        self.server_type = 'account'

        if self.container:
            self.server_type = 'container'
            self.container_path = os.path.join(self.account_path,
                                               self.container)
            self.keys['container'] = self.keymaster.create_key(
                self.container_path)

            if self.obj:
                self.server_type = 'object'
                self.obj_path = os.path.join(self.container_path, self.obj)
                self.keys['object'] = self.keymaster.create_key(
                    self.obj_path)

    def _handle_post_or_put(self, req, start_response):
        req.environ['swift.crypto.fetch_crypto_keys'] = self.fetch_crypto_keys
        resp = self._app_call(req.environ)
        start_response(self._response_status, self._response_headers,
                       self._response_exc_info)
        return resp

    def PUT(self, req, start_response):
        if self.obj_path:
            # TODO: re-examine need for this special handling once COPY has
            # been moved to middleware.
            # For object PUT we save a key_id as obj sysmeta so that if the
            # object is copied to another location we can use the key_id
            # (rather than its new path) to calculate its key for a GET or
            # HEAD.
            id_name = "%scrypto-id" % get_obj_persisted_sysmeta_prefix()
            req.headers[id_name] = \
                base64.b64encode(self.obj_path)

        return self._handle_post_or_put(req, start_response)

    def POST(self, req, start_response):
        return self._handle_post_or_put(req, start_response)

    def GET(self, req, start_response):
        return self._handle_get_or_head(req, start_response)

    def HEAD(self, req, start_response):
        return self._handle_get_or_head(req, start_response)

    def _handle_get_or_head(self, req, start_response):
        resp = self._app_call(req.environ)
        self.provide_keys_get_or_head(req)
        start_response(self._response_status, self._response_headers,
                       self._response_exc_info)
        return resp

    def error_if_need_keys(self, req):
        # Determine if keys will actually be needed
        # Look for any "x-<server_type>-sysmeta-crypto-meta" headers
        if not hasattr(self, '_response_headers'):
            return
        if any(strip_sys_meta_prefix(self.server_type, h).lower().startswith(
                'crypto-meta-')
               for (h, v) in self._response_headers
               if is_sys_meta(self.server_type, h)):
            self.logger.error("Cannot get necessary keys for path %s" %
                              req.path)
            raise HTTPUnprocessableEntity(
                "Cannot get necessary keys for path %s" % req.path)

        self.logger.debug("No encryption keys necessary for path %s" %
                          req.path)

    def provide_keys_get_or_head(self, req):
        if self.obj_path:
            # TODO: re-examine need for this special handling once COPY has
            # been moved to middleware.
            # For object GET or HEAD we look for a key_id that may have been
            # stored in the object sysmeta during a PUT and use that to
            # calculate the object key, in case the object has been copied to a
            # new path.
            try:
                id_name = \
                    "%scrypto-id" % get_obj_persisted_sysmeta_prefix()
                obj_key_path = self._response_header_value(id_name)
                if not obj_key_path:
                    raise ValueError('No object key was found.')
                try:
                    obj_key_path = base64.b64decode(obj_key_path)
                except TypeError:
                    self.logger.warn("path %s could not be decoded" %
                                     obj_key_path)
                    raise ValueError("path %s could not be decoded" %
                                     obj_key_path)
                path_acc, path_cont, path_obj = \
                    split_path(obj_key_path, 3, 3, True)
                cont_key_path = os.path.join(os.sep, path_acc, path_cont)
                self.keys['container'] = self.keymaster.create_key(
                    cont_key_path)
                self.logger.debug("obj key id: %s" % obj_key_path)
                self.logger.debug("cont key id: %s" % cont_key_path)
                self.keys['object'] = self.keymaster.create_key(
                    obj_key_path)
            except ValueError:
                req.environ['swift.crypto.override'] = True
                # TODO: uncomment when FakeFooters has been replaced with
                # real footer support. Fake Footers will insert crypto sysmeta
                # headers into all responses including 4xx that may have been
                # generated in the proxy (e.g. auth failures). This will cause
                # error_if_need_keys to replace the expected 4xx with a 422.
                # So disable the check for now.
                # self.error_if_need_keys(req)

        if not req.environ.get('swift.crypto.override'):
            req.environ['swift.crypto.fetch_crypto_keys'] = \
                self.fetch_crypto_keys

    def fetch_crypto_keys(self):
        return self.keys


class TrivialKeyMaster(object):
    """
    Encryption keymaster middleware for testing.  Don't use in production.
    """
    def __init__(self, app, conf):
        self.app = app
        self.logger = get_logger(conf, log_route="trivial_keymaster")
        # TODO: consider optionally loading root key from conf
        self.root_key = 'secret'.encode('utf-8')

    def __call__(self, env, start_response):
        req = Request(env)

        try:
            parts = req.split_path(2, 4, True)
        except ValueError:
            return self.app(env, start_response)

        if hasattr(TrivialKeyMasterContext, req.method):
            # handle only those request methods that may require keys
            km_context = TrivialKeyMasterContext(self, *parts[1:])
            try:
                return getattr(km_context, req.method)(req, start_response)
            except HTTPException as err_resp:
                return err_resp(env, start_response)

        # anything else
        return self.app(env, start_response)

    def create_key(self, key_id):
        return hmac.new(self.root_key, key_id,
                        digestmod=hashlib.sha256).digest()


def filter_factory(global_conf, **local_conf):
    conf = global_conf.copy()
    conf.update(local_conf)

    def trivial_keymaster_filter(app):
        return TrivialKeyMaster(app, conf)

    return trivial_keymaster_filter
