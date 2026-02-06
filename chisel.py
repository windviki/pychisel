#!/usr/bin/env python3
"""
PyChisel - Python Chisel Client

纯Python实现的chisel客户端，与jpillora/chisel服务器完全兼容。
"""

# Standard library imports
import argparse
import json
import logging
import os
import select
import socket
import struct
import sys
import threading
import time
from typing import List, Optional, Tuple, Dict, Any

# Third-party imports
import paramiko
import paramiko.common
from paramiko.message import Message
from paramiko.common import cMSG_GLOBAL_REQUEST
import websocket

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("PyChisel")

# Protocol constants
CHISEL_PROTOCOL_VERSION = "chisel-v3"
CHISEL_CLIENT_VERSION = "0.0.0-src"
CHISEL_CHANNEL_TYPE = "chisel"
SSH_VERSIONS_SUPPORTED = ("1.99", "2.0", "chisel")

# Connection defaults
DEFAULT_BANNER_TIMEOUT = 30


class WebSocketAdapter:
    """
    WebSocket Socket 适配器。
    允许 Paramiko 在 WebSocket 上运行。
    实现流式读取以处理SSH协议的分包需求。
    """

    def __init__(self, url, headers=None):
        self.url = url
        self.headers = headers or {}
        self.ws = None
        self._closed = False
        self._timeout = None
        self._buffer = b''  # 缓冲区用于处理分块读取
        self._blocking = True  # 阻塞模式标志

    def connect(self, *args, **kwargs):
        # Paramiko 传入的 address 被忽略，我们使用初始化时的 URL
        logger.info(f"Connecting via WebSocket: {self.url}")
        # 关键修复：禁用ping超时，让WebSocket的recv()能够持续阻塞
        # ping_interval=None表示不发送ping
        # ping_timeout=None表示不设置超时
        self.ws = websocket.WebSocket(
            ping_interval=None,  # 禁用ping
            ping_timeout=None,   # 禁用超时
        )
        # 手动添加Sec-WebSocket-Protocol头部（服务器需要这个头部，但不返回响应）
        headers = dict(self.headers)
        headers['Sec-WebSocket-Protocol'] = CHISEL_PROTOCOL_VERSION
        # 不使用subprotocols参数，避免验证失败
        # 关键修复：传递timeout=None禁用连接超时，避免socket在空闲时超时
        self.ws.connect(self.url, header=headers, timeout=None)
        self._closed = False

        # 关键：确保底层socket是阻塞模式且没有超时
        if hasattr(self.ws, 'sock') and self.ws.sock:
            self.ws.sock.setblocking(True)
            self.ws.sock.settimeout(None)  # 无限阻塞，永不超时

    def send(self, data):
        if self._closed:
            raise EOFError("Socket closed")
        # 必须发送二进制帧
        try:
            return self.ws.send(data, opcode=websocket.ABNF.OPCODE_BINARY)
        except Exception as e:
            raise socket.error(str(e))

    def recv(self, bufsize):
        """
        实现流式读取，处理SSH协议的分包需求。
        关键：确保阻塞模式下的正确行为。
        """
        # 调试日志
        thread_name = threading.current_thread().name
        logger.debug(f"[{thread_name}] WebSocketAdapter.recv() called, bufsize={bufsize}")

        if self._closed:
            logger.debug(f"[{thread_name}] WebSocketAdapter.recv() -> closed, returning b''")
            return b''

        # 如果缓冲区有数据，直接返回（不需要等待）
        if len(self._buffer) >= bufsize:
            result = self._buffer[:bufsize]
            self._buffer = self._buffer[bufsize:]
            logger.debug(f"[{thread_name}] WebSocketAdapter.recv() -> {len(result)} bytes from buffer")
            return result

        # 缓冲区数据不足，读取新的WebSocket消息
        if len(self._buffer) > 0:
            # 返回缓冲区的剩余数据
            result = self._buffer
            self._buffer = b''
            logger.debug(f"[{thread_name}] WebSocketAdapter.recv() -> {len(result)} bytes (remaining buffer)")
            return result

        # 读取新的WebSocket消息
        # 关键：只尝试一次，超时则抛出异常让上层处理
        try:
            logger.debug(f"[{thread_name}] WebSocketAdapter.recv() -> calling WebSocket.recv()...")
            # 读取WebSocket消息（阻塞直到有数据）
            data = self.ws.recv()

            if isinstance(data, str):
                data = data.encode('utf-8')

            # 成功读取数据
            if len(data) > bufsize:
                # 消息过大，保存剩余部分到缓冲区
                result = data[:bufsize]
                self._buffer = data[bufsize:]
                logger.debug(f"[{thread_name}] WebSocketAdapter.recv() -> {len(result)} bytes (buffered {len(self._buffer)})")
                return result
            else:
                # 返回整个消息
                logger.debug(f"[{thread_name}] WebSocketAdapter.recv() -> {len(data)} bytes")
                return data

        except websocket.WebSocketTimeoutException as e:
            # 超时：抛出异常而不是返回空数据
            # 让Paramiko知道这是一个临时错误
            logger.debug(f"[{thread_name}] WebSocketAdapter.recv() -> timeout, raising socket.timeout")
            raise socket.timeout("WebSocket read timeout") from e
        except websocket.WebSocketConnectionClosedException as e:
            # 连接真正关闭，返回空数据（EOF）
            self._closed = True
            logger.debug(f"[{thread_name}] WebSocketAdapter.recv() -> connection closed (EOF)")
            return b''
        except socket.timeout:
            # Socket超时：直接抛出
            logger.debug(f"[{thread_name}] WebSocketAdapter.recv() -> socket timeout, re-raising")
            raise
        except Exception as e:
            # 其他错误：可能是连接问题
            logger.debug(f"[{thread_name}] WebSocketAdapter.recv() -> error: {e}")
            self._closed = True
            return b''

    def close(self):
        if not self._closed and self.ws:
            self.ws.close()
            self._closed = True

    def setblocking(self, flag):
        """
        设置阻塞模式。
        Paramiko可能会调用这个方法来切换阻塞/非阻塞模式。
        """
        self._blocking = flag
        # 如果底层socket可用，也设置它的阻塞模式
        if hasattr(self.ws, 'sock') and self.ws.sock:
            self.ws.sock.setblocking(flag)

    def settimeout(self, timeout):
        """
        设置超时时间。
        Paramiko使用这个方法来设置读取超时。
        """
        self._timeout = timeout
        # 如果底层socket可用，也设置它的超时
        if hasattr(self.ws, 'sock') and self.ws.sock:
            self.ws.sock.settimeout(timeout)

    def gettimeout(self):
        return self._timeout

    def fileno(self):
        """
        返回底层socket的文件描述符。
        Paramiko的Transport内部线程使用select()监听这个fd。
        """
        # 返回底层WebSocket socket的文件描述符
        if hasattr(self.ws, 'sock') and self.ws.sock:
            return self.ws.sock.fileno()
        # 如果没有底层socket，抛出异常
        raise OSError("WebSocket not connected")


class ChiselServerInterface(paramiko.ServerInterface):
    """自定义ServerInterface用于处理chisel服务器发起的通道请求"""

    def __init__(self, client):
        self.client = client

    def check_channel_request(self, kind, chanid):
        """服务器尝试打开通道时调用"""
        logger.info(f"Server requested channel type: {kind}, id: {chanid}")
        if kind == CHISEL_CHANNEL_TYPE:
            # 接受chisel类型的通道
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_pty_request(self, channel, term, width, height, pixelw, pixelh, modes):
        """PTY请求 - 总是拒绝"""
        return False

    def check_channel_shell_request(self, channel):
        """Shell请求 - 总是拒绝"""
        return False

    def check_auth_publickey(self, username, key):
        """公钥认证 - 不支持"""
        return paramiko.AUTH_FAILED

    def check_auth_password(self, username, password):
        """密码认证 - 不支持（客户端不认证服务器）"""
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        """允许的认证方法"""
        return ""


class ChiselTransport(paramiko.Transport):
    """自定义Transport类，接受chisel协议版本"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.server_interface = None  # 稍后设置

    def _check_banner(self):
        # 读取服务器banner
        for i in range(100):
            timeout = self.banner_timeout if i == 0 else 2
            try:
                buf = self.packetizer.readline(timeout)
            except Exception as e:
                raise paramiko.SSHException("Error reading SSH protocol banner" + str(e))
            if buf[:4] == "SSH-":
                break
            self._log(paramiko.common.DEBUG, "Banner: " + buf)

        if buf[:4] != "SSH-":
            raise paramiko.SSHException('Indecipherable protocol version "' + buf + '"')

        self.remote_version = buf
        self._log(paramiko.common.DEBUG, "Remote version/idstring: {}".format(buf))

        # 解析版本
        i = buf.find(" ")
        if i >= 0:
            buf = buf[:i]
        segs = buf.split("-", 2)
        if len(segs) < 3:
            raise paramiko.SSHException("Invalid SSH banner")

        version = segs[1]
        client = segs[2]

        # 关键修改：接受chisel版本
        if version not in SSH_VERSIONS_SUPPORTED:
            raise paramiko.IncompatiblePeer(f"Incompatible version ({version} instead of {SSH_VERSIONS_SUPPORTED})")

        msg = "Connected (version {}, client {})".format(version, client)
        self._log(paramiko.common.INFO, msg)

    def _server_check_channel_request(self, kind, chanid):
        """重写服务器通道请求检查"""
        if self.server_interface:
            result = self.server_interface.check_channel_request(kind, chanid)
            logger.info(f"Channel request check: kind={kind}, chanid={chanid}, result={result}")
            return result
        # 默认拒绝所有非标准通道类型
        if kind == CHISEL_CHANNEL_TYPE:
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def _parse_channel_open(self, m):
        """重写通道打开解析，用于接受服务器发起的通道"""
        kind = m.get_text()
        chanid = m.get_int()
        window_size = m.get_int()
        max_packet_size = m.get_int()

        logger.info(f"Received CHANNEL_OPEN: kind={kind}, chanid={chanid}")

        # 检查是否接受此通道
        if self._server_check_channel_request(kind, chanid) != paramiko.OPEN_SUCCEEDED:
            logger.warning(f"Rejecting channel: kind={kind}")
            # 发送拒绝消息
            msg = Message()
            msg.add_byte(paramiko.common.cMSG_CHANNEL_OPEN_FAILURE)
            msg.add_int(chanid)
            msg.add_int(paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED)
            msg.add_string("")
            msg.add_string("en")
            self._send_user_message(msg)
            return

        # 接受通道
        logger.info(f"Accepting channel: kind={kind}, chanid={chanid}")
        chan = paramiko.Channel(chanid)
        self._channels.put(chanid, chan)
        self.channel_events[chanid] = threading.Event()
        self.channels_seen[chanid] = True
        chan._set_transport(self)
        chan._set_window(window_size, max_packet_size)

        # 发送成功消息
        msg = Message()
        msg.add_byte(paramiko.common.cMSG_CHANNEL_OPEN_SUCCESS)
        msg.add_int(chanid)
        msg.add_int(chanid)  # sender channel id
        msg.add_int(window_size)  # initial window size
        msg.add_int(max_packet_size)  # max packet size
        self._send_user_message(msg)

        # 对于reverse转发，立即启动数据处理
        # 从remotes_config获取目标地址
        if hasattr(self, 'server_interface') and hasattr(self.server_interface, 'client'):
            client = self.server_interface.client
            if client.remotes_config:
                for remote_str in client.remotes_config:
                    if remote_str.startswith("R:"):
                        # 从配置中提取目标信息
                        # 格式: R:server_port:client_host:client_port
                        parts = remote_str[2:].split(":")
                        if len(parts) >= 3:
                            target_host = parts[1]
                            target_port = int(parts[2])
                            logger.info(f"Connecting to target: {target_host}:{target_port}")

                            # 连接到本地目标
                            local_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                            try:
                                local_sock.connect((target_host, target_port))
                                logger.info(f"Connected to local target {target_host}:{target_port}")

                                # 启动数据泵
                                threading.Thread(
                                    target=client._pump,
                                    args=(chan, local_sock),
                                    daemon=True
                                ).start()
                            except Exception as e:
                                logger.error(f"Failed to connect to {target_host}:{target_port}: {e}")
                                chan.close()
                            break


class ChiselClient:
    """Chisel客户端主类"""

    def __init__(self, server: str, auth: Tuple[str, str], fingerprint: Optional[str] = None) -> None:
        """
        初始化Chisel客户端

        Args:
            server: Chisel服务器地址 (ws://host:port)
            auth: 认证信息 (username, password)
            fingerprint: 服务器主机密钥指纹（可选）
        """
        self.server = server
        self.username, self.password = auth
        self.fingerprint = fingerprint
        self.transport = None
        self.ssh_client = None
        self.active_threads: List[threading.Thread] = []
        self.remotes_config: List[str] = []  # 存储远程配置

        # 默认 headers
        self.headers: Dict[str, str] = {
            "User-Agent": "py-chisel/1.0"
        }

    @staticmethod
    def _format_fingerprint(key_bytes: bytes) -> str:
        """格式化指纹为十六进制字符串"""
        return ":".join(["{:02x}".format(b) for b in key_bytes])

    def run(self):
        # 1. 建立 WebSocket 连接
        ws_sock = WebSocketAdapter(self.server, headers=self.headers)
        ws_sock.connect()

        # 2. 启动 SSH Transport - 需要修改paramiko的版本检查
        self.transport = ChiselTransport(ws_sock)
        self.transport.use_compression(True)
        # 设置客户端版本（虽然服务器可能不严格验证）
        self.transport._client_version = f"SSH-{CHISEL_PROTOCOL_VERSION}-client"
        # 创建并设置ServerInterface实例用于处理服务器的通道请求
        server_interface = ChiselServerInterface(self)
        self.transport.server_interface = server_interface
        self.server_interface = server_interface  # 保存引用

        try:
            self.transport.start_client()
        except Exception as e:
            logger.error(f"SSH Handshake failed: {e}")
            return

        # 3. 主机密钥验证
        server_key = self.transport.get_remote_server_key()
        logger.info(f"Server host key: {server_key.get_name()}")

        if self.fingerprint:
            # 指纹比对
            fp_str = self._format_fingerprint(server_key.get_fingerprint())
            if fp_str != self.fingerprint:
                logger.error(f"Host key mismatch! Expected: {self.fingerprint}, Got: {fp_str}")
                self.transport.close()
                return

        # 4. 认证
        try:
            self.transport.auth_password(self.username, self.password)
            logger.info("Authentication successful.")
        except paramiko.AuthenticationException:
            logger.error("Authentication failed.")
            self.transport.close()
            return

        # 注意：不在这里发送配置，而是在添加转发规则后发送

        # 5. 启动SSH全局请求处理线程
        threading.Thread(target=self._handle_global_requests, daemon=True).start()

        # 6. 启动SSH通道处理线程
        threading.Thread(target=self._handle_channels, daemon=True).start()

        # 7. 关键修复：确保Transport的内部线程正在运行
        # Paramiko的Transport有一个内部线程来处理SSH消息
        # 我们需要确保这个线程在运行，否则无法处理服务器的响应
        logger.info("Checking if Transport internal thread is running...")
        if hasattr(self.transport, 'thread') and self.transport.thread:
            logger.info(f"Transport internal thread is active: {self.transport.thread.is_alive()}")
        else:
            logger.warning("Transport internal thread may not be running properly")

    def send_config_now(self):
        """发送Chisel配置（在添加转发规则后调用）"""
        # 立即发送配置，不等待
        self._send_config()

    def _parse_remote_string(self, remote_str):
        """解析远程配置字符串为Remote对象"""
        # 格式: "local_port:remote_host:remote_port" 或 "R:server_port:client_host:client_port" 或 "...:socks"
        if remote_str.startswith("R:"):
            # 反向转发: R:server_port:client_host:client_port
            # 服务器监听server_port，转发到客户端的client_host:client_port
            parts = remote_str[2:].split(":")
            if len(parts) >= 3:
                return {
                    "LocalHost": "0.0.0.0",  # 服务器监听地址
                    "LocalPort": parts[0],   # 服务器端口
                    "LocalProto": "tcp",
                    "RemoteHost": parts[1] if len(parts) > 1 else "127.0.0.1",  # 客户端目标主机
                    "RemotePort": parts[2] if len(parts) > 2 else parts[0],  # 客户端目标端口
                    "RemoteProto": "tcp",
                    "Socks": False,
                    "Reverse": True,
                    "Stdio": False
                }
        elif ":socks" in remote_str or remote_str.endswith(":socks"):
            # SOCKS代理: local_port:socks 或 127.0.0.1:port:socks
            parts = remote_str.split(":")
            return {
                "LocalHost": "127.0.0.1",
                "LocalPort": parts[0] if len(parts) == 2 else parts[1],
                "LocalProto": "tcp",
                "RemoteHost": "",
                "RemotePort": "",
                "RemoteProto": "tcp",
                "Socks": True,
                "Reverse": False,
                "Stdio": False
            }
        else:
            # 正向转发: local_port:remote_host:remote_port
            parts = remote_str.split(":")
            if len(parts) == 3:
                return {
                    "LocalHost": "0.0.0.0",
                    "LocalPort": parts[0],
                    "LocalProto": "tcp",
                    "RemoteHost": parts[1],
                    "RemotePort": parts[2],
                    "RemoteProto": "tcp",
                    "Socks": False,
                    "Reverse": False,
                    "Stdio": False
                }
        return None

    def _send_config(self):
        """发送Chisel配置给服务器"""
        # 将字符串数组转换为Remote对象数组
        remotes_array = []
        for remote_str in self.remotes_config:
            remote_obj = self._parse_remote_string(remote_str)
            if remote_obj:
                remotes_array.append(remote_obj)

        config = {
            "Version": CHISEL_CLIENT_VERSION,
            "Remotes": remotes_array
        }
        # 关键修复：使用紧凑的JSON格式（无空格），与Go的json.Marshal一致
        config_json = json.dumps(config, separators=(',', ':')).encode('utf-8')
        logger.info(f"Sending config: {config_json.decode('utf-8')}")

        try:
            # ========== 关键修复：手动构建GLOBAL_REQUEST消息 ==========
            # 问题：Paramiko的global_request对bytes添加4字节长度前缀，导致双重封装
            # 解决方案：手动构建消息，避免双重长度前缀

            # SSH GLOBAL_REQUEST消息格式：
            # byte: SSH_MSG_GLOBAL_REQUEST (80 = 0x50)
            # string: "config" (4字节长度 + 数据)
            # boolean: want_reply (1)
            # string: payload (4字节长度 + JSON数据) <- 关键：只添加一次长度前缀

            # 构建消息
            m = Message()
            m.add_byte(cMSG_GLOBAL_REQUEST)
            m.add_string("config")  # 添加 "config"（会添加4字节长度前缀）
            m.add_boolean(True)     # 添加 want_reply = true

            # 获取当前消息
            partial_msg = m.asbytes()

            # 关键修复：payload应该是原始JSON，不带长度前缀！
            # Go服务器的实现是直接读取payload字段作为JSON，而不是作为string类型
            # SSH协议中，request type和want_reply之后的部分就是raw payload
            complete_msg_bytes = partial_msg + config_json

            logger.debug(f"Complete GLOBAL_REQUEST message ({len(complete_msg_bytes)} bytes):")
            logger.debug(f"  Hex (first 100): {complete_msg_bytes.hex()[:100]}")

            # 创建Message对象并发送
            complete_msg = Message(complete_msg_bytes)

            # 设置completion_event用于等待响应
            self.transport.completion_event = threading.Event()
            self.transport.completion_event.clear()
            self.transport.global_response = None

            # 发送消息
            self.transport._send_user_message(complete_msg)

            # 等待响应（最多10秒）
            for _ in range(100):
                if self.transport.completion_event.is_set():
                    break
                time.sleep(0.1)

            result = self.transport.global_response

            if result is None:
                logger.error(f"Config failed: Server rejected the request or timeout")
                raise Exception("Server rejected config request")
            elif isinstance(result, paramiko.Message):
                # 成功！
                logger.info(f"Config sent successfully, server accepted the config")
            else:
                logger.warning(f"Unexpected response type: {type(result)}")
        except Exception as e:
            logger.error(f"Failed to send config: {e}")
            raise

    def _handle_global_requests(self):
        """处理SSH全局请求（如ping）"""
        while self.transport.is_active():
            try:
                req = self.transport._global_request_queue.get(timeout=1)
                if req:
                    # 处理ping请求
                    if req[0] == "ping":
                        req[1](True, b"pong")
            except:
                pass

    def _handle_channels(self):
        """处理传入的SSH通道（用于反向转发）

        当reverse转发时，服务器会主动打开SSH通道连接到客户端。
        这个函数接受这些通道并转发到本地目标。
        """
        while self.transport.is_active():
            try:
                chan = self.transport.accept(1)
                if chan:
                    logger.info(f"Channel accepted from server: {chan.get_name()}")
                    # 获取通道目标地址
                    # 对于chisel通道，目标地址在通道创建时的extra_data中
                    try:
                        # 从remotes_config中查找reverse规则
                        if self.remotes_config:
                            for remote_str in self.remotes_config:
                                if remote_str.startswith("R:"):
                                    # 从配置中提取目标信息
                                    # 格式: R:server_port:client_host:client_port
                                    parts = remote_str[2:].split(":")
                                    if len(parts) >= 3:
                                        target_host = parts[1]
                                        target_port = int(parts[2])
                                        logger.info(f"Connecting to target: {target_host}:{target_port}")

                                        # 连接到本地目标
                                        local_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                                        try:
                                            local_sock.connect((target_host, target_port))
                                            logger.info(f"Connected to local target {target_host}:{target_port}")

                                            # 启动数据泵
                                            threading.Thread(
                                                target=self._pump,
                                                args=(chan, local_sock),
                                                daemon=True
                                            ).start()
                                            break  # 只处理第一个reverse规则
                                        except Exception as e:
                                            logger.error(f"Failed to connect to {target_host}:{target_port}: {e}")
                                            chan.close()
                    except Exception as e:
                        logger.error(f"Error handling reverse channel: {e}")
                        import traceback
                        logger.error(traceback.format_exc())
                        chan.close()
            except Exception as e:
                if self.transport.is_active():
                    logger.debug(f"Error in _handle_channels: {e}")
                pass

    def start_forward(self, local_port, remote_host, remote_port):
        """启动本地转发 (-L)"""
        logger.info(f"Setting up Local Forward: localhost:{local_port} -> {remote_host}:{remote_port}")
        # 不在这里添加配置，而是在main函数中统一添加

        t = threading.Thread(
            target=self._local_forward_loop,
            args=(local_port, remote_host, remote_port),
            daemon=True
        )
        t.start()
        self.active_threads.append(t)

    def start_remote_forward(self, server_port, client_host, client_port):
        """启动远程转发 (-R)

        对于reverse转发，配置已经通过SSH全局请求发送给服务器。
        服务器会监听server_port，当有连接时，服务器会主动打开SSH通道连接到客户端。
        _handle_channels线程会接受这些通道并转发到client_host:client_port。
        """
        logger.info(f"Setting up Remote Forward: Server listening on :{server_port} -> {client_host}:{client_port}")
        # 注意：不需要发送tcpip-forward请求，Go chisel使用自定义协议
        # 服务器已经通过配置知道了要监听哪个端口
        # _handle_channels线程已经在运行，会自动处理服务器的反向连接
        # 不需要启动_remote_forward_loop，因为服务器会主动打开连接

    def start_socks(self, local_port):
        """启动 SOCKS5 代理 (--socks)"""
        logger.info(f"Setting up SOCKS5 proxy on localhost:{local_port}")
        # 不在这里添加配置，而是在main函数中统一添加

        t = threading.Thread(
            target=self._socks_loop,
            args=(local_port,),
            daemon=True
        )
        t.start()
        self.active_threads.append(t)

    def _local_forward_loop(self, local_port, remote_host, remote_port):
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server_sock.bind(('127.0.0.1', local_port))
            server_sock.listen(100)
            logger.info(f"Listening on 127.0.0.1:{local_port}")
        except Exception as e:
            logger.error(f"Failed to bind port {local_port}: {e}")
            return

        while True:
            try:
                client_sock, addr = server_sock.accept()
                logger.info(f"Accepted local connection from {addr}")
                # 开启线程处理此连接
                threading.Thread(
                    target=self._forward_data,
                    args=(client_sock, remote_host, remote_port),
                    daemon=True
                ).start()
            except Exception as e:
                logger.error(f"Local forward accept error: {e}")
                break

    def _forward_data(self, local_sock, remote_host, remote_port):
        # 连接远程 - 使用chisel通道类型
        target = f"{remote_host}:{remote_port}"
        chan = None
        try:
            logger.info(f"Opening SSH channel to {target}")

            # chisel服务器期望自定义的"chisel"通道类型
            # 目标地址作为额外数据（ExtraData）传递

            # 手动构建SSH CHANNEL_OPEN消息
            self.transport.lock.acquire()
            try:
                chanid = self.transport._next_channel()
                window_size = self.transport._sanitize_window_size(None)
                max_packet_size = self.transport._sanitize_packet_size(None)

                # 构建CHANNEL_OPEN消息
                # SSH协议格式: byte(type) + string(channel_type) + int(channel_id) +
                #               int(window_size) + int(max_packet_size) + byte[](extra_data)
                # 注意：extra数据是原始字节，不带长度前缀！
                m = Message()
                m.add_byte(bytes([paramiko.common.MSG_CHANNEL_OPEN]))
                m.add_string("chisel")  # 通道类型，add_string会添加4字节长度前缀（正确）
                m.add_int(chanid)
                m.add_int(window_size)
                m.add_int(max_packet_size)
                # 关键修复：额外数据直接附加，不使用add_string()避免双重长度前缀
                partial_msg = m.asbytes()
                complete_msg_bytes = partial_msg + target.encode('utf-8')

                # 创建Channel对象并注册到Transport
                chan = paramiko.Channel(chanid)
                self.transport._channels.put(chanid, chan)
                self.transport.channel_events[chanid] = event = threading.Event()
                self.transport.channels_seen[chanid] = True
                chan._set_transport(self.transport)
                chan._set_window(window_size, max_packet_size)

                # 发送CHANNEL_OPEN消息（使用完整消息）
                complete_msg = Message(complete_msg_bytes)
                self.transport._send_user_message(complete_msg)
            finally:
                self.transport.lock.release()

            # 等待服务器响应CHANNEL_OPEN_CONFIRMATION
            logger.info(f"Waiting for channel open confirmation...")
            start_ts = time.time()
            timeout = 30
            while True:
                if event.wait(0.1):
                    break
                if not self.transport.is_active():
                    raise paramiko.ssh_exception.SSHException("Transport closed while opening channel.")
                if start_ts + timeout < time.time():
                    raise paramiko.ssh_exception.SSHException(f"Timeout opening channel after {timeout}s.")

            # 获取已打开的channel
            chan = self.transport._channels.get(chanid)
            logger.info(f"SSH channel opened (chisel) to {target}")

        except Exception as e:
            logger.error(f"Failed to open SSH channel: {e}")
            logger.error(f"Exception type: {type(e).__name__}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            try:
                local_sock.close()
            except:
                pass
            return

        # 开始泵送数据
        logger.info(f"Starting data pump for {target}")
        self._pump(chan, local_sock)

    def _remote_forward_loop(self, remote_port, local_host, local_port):
        while True:
            chan = self.transport.accept(20) # 20s timeout
            if chan is None:
                continue

            origin = chan.getpeername()
            logger.info(f"Remote connection accepted: {origin} -> :{remote_port}")

            local_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                local_sock.connect((local_host, local_port))
            except Exception as e:
                logger.error(f"Failed to connect to local target {local_host}:{local_port}: {e}")
                chan.close()
                continue

            threading.Thread(
                target=self._pump,
                args=(chan, local_sock),
                daemon=True
            ).start()

    def _socks_loop(self, local_port):
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server_sock.bind(('127.0.0.1', local_port))
            server_sock.listen(100)
        except Exception as e:
            logger.error(f"Failed to bind SOCKS port {local_port}: {e}")
            return

        logger.info(f"SOCKS5 Proxy listening on 127.0.0.1:{local_port}")

        while True:
            client_sock, addr = server_sock.accept()
            threading.Thread(
                target=self._socks_handler,
                args=(client_sock,),
                daemon=True
            ).start()

    def _socks_handler(self, client_sock):
        try:
            # 打开SSH通道，目标地址是"socks"
            # 服务器端会处理SOCKS协议
            try:
                chan = self.transport.open_channel(
                    "chisel",
                    "socks".encode('utf-8'),
                    timeout=30
                )
            except Exception as e:
                logger.error(f"Failed to open SOCKS channel: {e}")
                client_sock.close()
                return

            # 直接双向转发数据 - SOCKS协议由服务器端处理
            self._pump(chan, client_sock)

        except Exception as e:
            logger.error(f"SOCKS handler error: {e}")
            try:
                client_sock.close()
            except:
                pass

    def _pump(self, src, dst):
        """Pump data between src and dst (bidirectional) using two threads"""

        # 使用共享状态来跟踪连接
        state = {
            "src_to_dst_active": True,
            "dst_to_src_active": True,
            "lock": threading.Lock()
        }

        def forward(source, destination, name, is_src_to_dst):
            """Forward data from source to destination"""
            logger.info(f"Forward thread {name} started")
            try:
                # Set timeout for socket objects
                if hasattr(source, 'settimeout'):
                    try:
                        source.settimeout(1)
                    except:
                        pass

                while True:
                    try:
                        data = source.recv(4096)
                        if not data:
                            logger.info(f"{name}: EOF received")
                            break
                        logger.info(f"{name}: received {len(data)} bytes, forwarding...")
                        destination.sendall(data)
                        logger.debug(f"{name}: forwarded {len(data)} bytes")
                    except socket.timeout:
                        continue
                    except Exception as e:
                        logger.info(f"{name}: recv error - {type(e).__name__}: {e}")
                        break
            except Exception as e:
                logger.error(f"{name}: forward error - {type(e).__name__}: {e}")
            finally:
                # 只标记自己完成，不关闭 socket
                with state["lock"]:
                    if is_src_to_dst:
                        state["src_to_dst_active"] = False
                    else:
                        state["dst_to_src_active"] = False

                logger.info(f"{name}: completed")

                # 检查是否两个方向都完成了
                with state["lock"]:
                    both_done = not state["src_to_dst_active"] and not state["dst_to_src_active"]

                if both_done:
                    # 只有一个线程负责关闭
                    if is_src_to_dst:
                        logger.info(f"{name}: closing both sockets")
                        try:
                            src.close()
                        except:
                            pass
                        try:
                            dst.close()
                        except:
                            pass

        # Create two forwarding threads
        logger.info(f"Creating forward threads for data pump")
        t1 = threading.Thread(target=forward, args=(src, dst, "src->dst", True), daemon=True)
        t2 = threading.Thread(target=forward, args=(dst, src, "dst->src", False), daemon=True)

        t1.start()
        t2.start()

        # Wait for both threads to complete
        t1.join()
        t2.join()

        logger.info("Data pump completed")


def parse_forward(arg):
    """解析 -L 或 -R 参数: 8080:google.com:80"""
    parts = arg.split(":")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError(f"Invalid forward spec: {arg}. Format: port:host:port")
    return parts[0], parts[1], int(parts[2])


def main():
    parser = argparse.ArgumentParser(description="Python Chisel Client (SSH over WebSocket)")
    parser.add_argument("server", help="Chisel server URL (e.g., ws://localhost:8080)")
    parser.add_argument("--auth", help="User:Pass for authentication", required=True)
    parser.add_argument("--fingerprint", help="Server SSH public key fingerprint (SHA256 hex string)")

    # 转发参数
    parser.add_argument("-L", "--local", dest="locals", action="append", type=parse_forward,
                        help="Local port forward (e.g., -L 8080:remotehost:80)")
    parser.add_argument("-R", "--remote", dest="remotes", action="append", type=parse_forward,
                        help="Remote port forward (e.g., -R 8080:localhost:3000)")
    parser.add_argument("--socks", type=int, help="Start SOCKS5 proxy on local port")

    args = parser.parse_args()

    # 解析认证
    try:
        username, password = args.auth.split(":")
    except ValueError:
        logger.error("Auth must be in format user:pass")
        sys.exit(1)

    # 启动客户端
    client = ChiselClient(args.server, (username, password), args.fingerprint)

    # 在子线程中运行连接逻辑，主线程保持存活
    client.run()

    if not client.transport or not client.transport.is_active():
        logger.error("Failed to establish connection.")
        sys.exit(1)

    # 先配置转发规则（但不启动监听）
    if args.locals:
        for local_port, remote_host, remote_port in args.locals:
            remote_config = f"{local_port}:{remote_host}:{remote_port}"
            client.remotes_config.append(remote_config)

    if args.remotes:
        for server_port, client_host, client_port in args.remotes:
            # 格式: R:server_port:client_host:client_port
            remote_config = f"R:{server_port}:{client_host}:{client_port}"
            client.remotes_config.append(remote_config)

    if args.socks:
        socks_config = f"127.0.0.1:{args.socks}:socks"
        client.remotes_config.append(socks_config)

    # 现在发送配置（包含所有转发规则）
    client.send_config_now()

    # 然后启动监听
    if args.locals:
        for local_port, remote_host, remote_port in args.locals:
            client.start_forward(int(local_port), remote_host, remote_port)

    if args.remotes:
        for server_port, client_host, client_port in args.remotes:
            client.start_remote_forward(int(server_port), client_host, int(client_port))

    if args.socks:
        client.start_socks(args.socks)

    # 保持主线程运行，直到 Transport 断开
    try:
        while client.transport.is_active():
            # 定期检查连接状态
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        if client.transport:
            client.transport.close()


if __name__ == "__main__":
    main()
