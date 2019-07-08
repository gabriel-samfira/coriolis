# Copyright 2016 Cloudbase Solutions Srl
# All Rights Reserved.

import abc
import contextlib
import errno
import os
import tempfile
import threading

from oslo_config import cfg
from oslo_log import log as logging
import paramiko
import requests
from six import with_metaclass

from coriolis import data_transfer
from coriolis import exception
from coriolis import utils

LOG = logging.getLogger(__name__)
CONF = cfg.CONF
opts = [
    cfg.BoolOpt('compress_transfers',
                default=True,
                help='Use compression if possible during disk transfers'),
]
CONF.register_opts(opts)
_CORIOLIS_HTTP_WRITER_CMD = "coriolis-writer"


class BaseBackupWriterImpl(with_metaclass(abc.ABCMeta)):
    def __init__(self, path, disk_id):
        self._path = path
        self._disk_id = disk_id

    @abc.abstractmethod
    def _open(self):
        pass

    def _handle_exception(self, ex):
        LOG.exception(ex)

    @abc.abstractmethod
    def seek(self, pos):
        pass

    @abc.abstractmethod
    def truncate(self, size):
        pass

    @abc.abstractmethod
    def write(self, data):
        pass

    @abc.abstractmethod
    def close(self):
        pass


class BaseBackupWriter(with_metaclass(abc.ABCMeta)):
    @abc.abstractmethod
    def _get_impl(self, path, disk_id):
        pass

    @contextlib.contextmanager
    def open(self, path, disk_id):
        impl = None
        try:
            impl = self._get_impl(path, disk_id)
            impl._open()
            yield impl
        except Exception as ex:
            if impl:
                impl._handle_exception(ex)
            raise
        finally:
            if impl:
                impl.close()


class FileBackupWriterImpl(BaseBackupWriterImpl):
    def __init__(self, path, disk_id):
        self._file = None
        super(FileBackupWriterImpl, self).__init__(path, disk_id)

    def _open(self):
        # Create file if it doesn't exist
        open(self._path, 'ab+').close()
        self._file = open(self._path, 'rb+')

    def seek(self, pos):
        self._file.seek(pos)

    def truncate(self, size):
        self._file.truncate(size)

    def write(self, data):
        self._file.write(data)

    def close(self):
        self._file.close()
        self._file = None


class FileBackupWriter(BaseBackupWriter):
    def _get_impl(self, path, disk_id):
        return FileBackupWriterImpl(path, disk_id)


class SSHBackupWriterImpl(BaseBackupWriterImpl):
    def __init__(self, path, disk_id, compress_transfer=None):
        self._msg_id = None
        self._stdin = None
        self._stdout = None
        self._stderr = None
        self._offset = None
        self._ssh = None
        self._compress_transfer = compress_transfer
        if self._compress_transfer is None:
            self._compress_transfer = CONF.compress_transfers
        super(SSHBackupWriterImpl, self).__init__(path, disk_id)

    def _set_ssh_client(self, ssh):
        self._ssh = ssh

    @utils.retry_on_error()
    def _exec_helper_cmd(self):
        self._msg_id = 0
        self._offset = 0
        self._stdin, self._stdout, self._stderr = self._ssh.exec_command(
            "chmod +x write_data && sudo ./write_data")

    def _encode_data(self, content):
        msg = data_transfer.encode_data(
            self._msg_id, self._path,
            self._offset, content,
            compress=self._compress_transfer)

        LOG.debug(
            "Guest path: %(path)s, offset: %(offset)d, content len: "
            "%(content_len)d, msg len: %(msg_len)d",
            {"path": self._path,
             "offset": self._offset,
             "content_len": len(content),
             "msg_len": len(msg)})
        return msg

    def _encode_eod(self):
        msg = data_transfer.encode_eod(self._msg_id)
        LOG.debug("EOD message len: %d", len(msg))
        return msg

    @utils.retry_on_error()
    def _send_msg(self, data):
        self._msg_id += 1
        self._stdin.write(data)
        self._stdin.flush()
        self._stdout.read(4)

    def _open(self):
        self._exec_helper_cmd()

    def seek(self, pos):
        self._offset = pos

    def truncate(self, size):
        pass

    def write(self, data):
        self._send_msg(self._encode_data(data))
        self._offset += len(data)

    def close(self):
        if self._ssh:
            self._send_msg(self._encode_eod())
            self._ssh.close()
            self._ssh = None

    def _handle_exception(self, ex):
        super(SSHBackupWriterImpl, self)._handle_exception(ex)

        ret_val = None
        # if the application is still running on the other side,
        # recv_exit_status() will block. Check that we have an
        # exit status before retrieving it
        if self._stdout.channel.exit_status_ready():
            ret_val = self._stdout.channel.recv_exit_status()

        # Don't send a message via ssh on exception
        self._ssh.close()
        self._ssh = None

        if ret_val:
            # TODO(alexpilotti): map error codes to error messages
            raise exception.CoriolisException(
                "An exception occurred while writing data on target. "
                "Exit code: %s" % ret_val)
        else:
            raise exception.CoriolisException(
                "An exception occurred while writing data on target: %s" %
                ex)


class SSHBackupWriter(BaseBackupWriter):
    def __init__(self, ip, port, username, pkey, password, volumes_info):
        self._ip = ip
        self._port = port
        self._username = username
        self._pkey = pkey
        self._password = password
        self._volumes_info = volumes_info
        self._ssh = None
        self._lock = threading.Lock()

    def _get_impl(self, path, disk_id):
        ssh = self._connect_ssh()

        path = [v for v in self._volumes_info
                if v["disk_id"] == disk_id][0]["volume_dev"]
        impl = SSHBackupWriterImpl(path, disk_id)

        self._copy_helper_cmd(ssh)
        impl._set_ssh_client(ssh)
        return impl

    @utils.retry_on_error()
    def _copy_helper_cmd(self, ssh):
        with self._lock:
            sftp = ssh.open_sftp()
            local_path = os.path.join(
                utils.get_resources_dir(), 'write_data')
            try:
                # Check if the remote file already exists
                sftp.stat('write_data')
            except IOError as ex:
                if ex.errno != errno.ENOENT:
                    raise
                sftp.put(local_path, 'write_data')
            finally:
                sftp.close()

    @utils.retry_on_error()
    def _connect_ssh(self):
        LOG.info("Connecting to SSH host: %(ip)s:%(port)s" %
                 {"ip": self._ip, "port": self._port})
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(
            hostname=self._ip,
            port=self._port,
            username=self._username,
            pkey=self._pkey,
            password=self._password)
        return ssh


class HTTPBackupWriterImpl(BaseBackupWriterImpl):
    def __init__(self, path, disk_id, compress_transfer=None):
        self._offset = None
        self._session = None
        self._ip = None
        self._port = None
        self._crt = None
        self._key = None
        self._ca = None
        self._write_error = False
        self._profile = [{
            "compress_time": None,
            "write_time": None,
            "transferred_compressed": None,
            "transferred_inflated": None,
        }]
        self._compress_transfer = compress_transfer
        if self._compress_transfer is None:
            self._compress_transfer = CONF.compress_transfers
        super(HTTPBackupWriterImpl, self).__init__(path, disk_id)

    def _set_info(self, info):
        self._ip = info.get("ip")
        self._port = info.get("port")
        self._crt = info.get("client_crt")
        self._key = info.get("client_key")
        self._ca = info.get("ca_crt")
        if not all([self._ip, self._port, self._crt, self._key, self._ca]):
            raise exception.CoriolisException(
                "Missing required info when creating HTTPBackupWriter")

    @property
    def _uri(self):
        return "https://%s:%s/api/v1%s" % (
            self._ip, self._port, self._path
        )

    def _init_session(self):
        if self._session:
            self._session.close()
        sess = requests.Session()
        sess.cert = (
            self._crt,
            self._key)
        sess.verify = self._ca
        self._session = sess

    def _open(self):
        self._init_session()

    def seek(self, pos):
        self._offset = pos

    def truncate(self, size):
        pass

    def _ensure_session(self):
        if not self._session:
            self._init_session()
            return
        if self._write_error:
            self._init_session()
            return

    @utils.retry_on_error()
    def write(self, data):
        self._ensure_session()
        headers = {
            "X-Write-Offset": str(self._offset),
        }

        if self._compress_transfer:
            compressed, payload = data_transfer.gzip_data(data)
            if compressed:
                headers["content-encoding"] = 'gzip'
        else:
            compressed, payload = False, data

        try:
            LOG.debug(
                "Guest path: %(path)s, offset: %(offset)d, content len: "
                "%(content_len)d",
                {"path": self._path,
                 "offset": self._offset,
                 "content_len": len(data)})
            resp = self._session.post(
                self._uri, headers=headers, data=payload
            )
            LOG.debug("Response code: %r, content: %r" %
                      (resp.status_code, resp.content))
            resp.raise_for_status()
            self._offset += len(data)
            self._write_error = False
        except Exception as err:
            LOG.warning(
                "Error writing chunk to disk %s at offset"
                " %s: %s" % (self._path, self._offset, err))
            self._write_error = True
            raise

    def close(self):
        if self._session:
            self._session.close()
            self._session = None


class HTTPBackupWriter(BaseBackupWriter):

    def __init__(self, ip, port, username, pkey,
                 password, writer_port, volumes_info):
        self._ip = ip
        self._port = port
        self._username = username
        self._pkey = pkey
        self._password = password
        self._volumes_info = volumes_info
        self._writer_port = writer_port
        self._lock = threading.Lock()
        self._writer_cmd = os.path.join(
            "/usr/bin", _CORIOLIS_HTTP_WRITER_CMD)
        self._crt = None
        self._key = None
        self._ca = None
        self._crt_dir = None

    def _wait_for_conn(self):
        utils.wait_for_port_connectivity(
            self._ip, self._writer_port)

    def _inject_iptables_allow(self, ssh):
        utils.exec_ssh_cmd(
            ssh,
            "sudo /sbin/iptables -I INPUT -p tcp --dport %s "
            "-j ACCEPT" % self._writer_port)

    def _get_impl(self, path, disk_id):
        ssh = self._connect_ssh()
        self._setup_writer(ssh)

        path = [v for v in self._volumes_info
                if v["disk_id"] == disk_id][0]["volume_dev"]
        impl = HTTPBackupWriterImpl(path, disk_id)
        impl._set_info({
            "ip": self._ip,
            "port": self._writer_port,
            "client_crt": self._crt,
            "client_key": self._key,
            "ca_crt": self._ca,
        })
        return impl

    @utils.retry_on_error()
    def _copy_writer(self, ssh):
        local_path = os.path.join(
            utils.get_resources_dir(), _CORIOLIS_HTTP_WRITER_CMD)
        with self._lock:
            sftp = ssh.open_sftp()
            try:
                # Check if the remote file already exists
                sftp.stat(self._writer_cmd)
            except IOError as ex:
                if ex.errno != errno.ENOENT:
                    raise
                sftp.put(local_path, _CORIOLIS_HTTP_WRITER_CMD)
                utils.exec_ssh_cmd(
                    ssh,
                    "sudo mv %s %s" % (
                        _CORIOLIS_HTTP_WRITER_CMD, self._writer_cmd),
                    get_pty=True
                )
                utils.exec_ssh_cmd(
                    ssh,
                    "sudo chmod +x %s" % self._writer_cmd,
                    get_pty=True
                )
            finally:
                sftp.close()

    def _fetch_remote_file(self, ssh, remote_file, local_file):
        with open(local_file, 'wb') as fd:
            utils.exec_ssh_cmd(
                ssh,
                "sudo chmod +r %s" % remote_file, get_pty=True)
            data = utils.retry_on_error()(
                utils.read_ssh_file)(ssh, remote_file)
            fd.write(data)

    def _setup_certificates(self, ssh):
        remote_base_dir = "/etc/coriolis-writer"
        self._crt_dir = tempfile.mkdtemp()

        ca_crt_name = "ca-cert.pem"
        client_crt_name = "client-cert.pem"
        client_key_name = "client-key.pem"

        srv_crt_name = "srv-cert.pem"
        srv_key_name = "srv-key.pem"

        remote_ca_crt = os.path.join(remote_base_dir, ca_crt_name)
        remote_client_crt = os.path.join(remote_base_dir, client_crt_name)
        remote_client_key = os.path.join(remote_base_dir, client_key_name)
        remote_srv_crt = os.path.join(remote_base_dir, srv_crt_name)
        remote_srv_key = os.path.join(remote_base_dir, srv_key_name)

        ca_crt = os.path.join(self._crt_dir, ca_crt_name)
        client_crt = os.path.join(self._crt_dir, client_crt_name)
        client_key = os.path.join(self._crt_dir, client_key_name)

        exist = []
        for i in (remote_ca_crt, remote_client_crt, remote_client_key,
                  remote_srv_crt, remote_srv_key):
            exist.append(utils.test_ssh_path(ssh, i))

        if not all(exist):
            utils.exec_ssh_cmd(
                ssh, "sudo mkdir -p %s" % remote_base_dir, get_pty=True)
            utils.exec_ssh_cmd(
                ssh,
                "sudo %(writer_cmd)s generate-certificates -output-dir "
                "%(cert_dir)s -certificate-hosts %(extra_hosts)s" % {
                    "writer_cmd": self._writer_cmd,
                    "cert_dir": remote_base_dir,
                    "extra_hosts": self._ip,
                },
                get_pty=True)

            self._fetch_remote_file(ssh, remote_ca_crt, ca_crt)
            self._fetch_remote_file(ssh, remote_client_crt, client_crt)
            self._fetch_remote_file(ssh, remote_client_key, client_key)

        return {
            "local": {
                "client_crt": client_crt,
                "client_key": client_key,
                "ca_crt": ca_crt,
            },
            "remote": {
                "srv_crt": remote_srv_crt,
                "srv_key": remote_srv_key,
                "ca_crt": remote_ca_crt,
            },
        }

    def _init_writer(self, ssh, cert_paths):
        cmdline = ("%(cmd)s run -ca-cert %(ca_cert)s -key "
                   "%(srv_key)s -cert %(srv_cert)s -listen-port "
                   "%(listen_port)s") % {
                       "cmd": self._writer_cmd,
                       "ca_cert": cert_paths["ca_crt"],
                       "srv_key": cert_paths["srv_key"],
                       "srv_cert": cert_paths["srv_crt"],
                       "listen_port": self._writer_port,
            }
        utils.create_service(
            ssh, cmdline, _CORIOLIS_HTTP_WRITER_CMD, start=True)
        self._inject_iptables_allow(ssh)
        self._wait_for_conn()

    def _setup_writer(self, ssh):
        self._copy_writer(ssh)
        paths = utils.retry_on_error()(
            self._setup_certificates)(ssh)
        self._crt = paths["local"]["client_crt"]
        self._key = paths["local"]["client_key"]
        self._ca = paths["local"]["ca_crt"]
        utils.retry_on_error()(
            self._init_writer)(ssh, paths["remote"])

    @utils.retry_on_error()
    def _connect_ssh(self):
        LOG.info("Connecting to SSH host: %(ip)s:%(port)s" %
                 {"ip": self._ip, "port": self._port})
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(
            hostname=self._ip,
            port=self._port,
            username=self._username,
            pkey=self._pkey,
            password=self._password)
        return ssh
