# -*- coding: utf-8 -*-
"""Agora AccessToken2 (v007) implementation in pure Python.

Based on Agora's official open-source DynamicKey tools (AgoraIO/Tools).
No external binary dependencies required.
"""
from __future__ import annotations

import base64
from collections import OrderedDict
import hashlib
import hmac
import secrets
import struct
import time
import zlib

VERSION_LENGTH = 3


def get_version() -> str:
    return "007"


# --- Binary Packers ---


def pack_uint16(x: int) -> bytes:
    return struct.pack("<H", int(x))


def unpack_uint16(buffer: bytes) -> tuple[int, bytes]:
    length = struct.calcsize("H")
    return struct.unpack("<H", buffer[:length])[0], buffer[length:]


def pack_uint32(x: int) -> bytes:
    return struct.pack("<I", int(x))


def unpack_uint32(buffer: bytes) -> tuple[int, bytes]:
    length = struct.calcsize("I")
    return struct.unpack("<I", buffer[:length])[0], buffer[length:]


def pack_string(string: str | bytes) -> bytes:
    if isinstance(string, str):
        raw = string.encode("utf-8")
    else:
        raw = string
    return pack_uint16(len(raw)) + raw


def unpack_string(buffer: bytes) -> tuple[bytes, bytes]:
    data_length, buffer = unpack_uint16(buffer)
    return struct.unpack(f"<{data_length}s", buffer[:data_length])[0], buffer[data_length:]


def pack_map_uint32(m: dict[int, int]) -> bytes:
    return pack_uint16(len(m)) + b"".join(
        pack_uint16(k) + pack_uint32(v) for k, v in m.items()
    )


def unpack_map_uint32(buffer: bytes) -> tuple[dict[int, int], bytes]:
    length, buffer = unpack_uint16(buffer)
    data = {}
    for _ in range(length):
        k, buffer = unpack_uint16(buffer)
        v, buffer = unpack_uint32(buffer)
        data[k] = v
    return data, buffer


# --- Service Definitions ---


class Service:
    def __init__(self, service_type: int) -> None:
        self._type = service_type
        self._privileges: dict[int, int] = {}

    def add_privilege(self, privilege: int, expire: int) -> None:
        self._privileges[privilege] = expire

    def service_type(self) -> int:
        return self._type

    def pack(self) -> bytes:
        packed_type = pack_uint16(self._type)
        ordered_privileges = OrderedDict(
            sorted(self._privileges.items(), key=lambda x: int(x[0]))
        )
        return packed_type + pack_map_uint32(ordered_privileges)


class ServiceRtc(Service):
    kServiceType = 1

    kPrivilegeJoinChannel = 1
    kPrivilegePublishAudioStream = 2
    kPrivilegePublishVideoStream = 3
    kPrivilegePublishDataStream = 4

    def __init__(self, channel_name: str = "", uid: int | str = 0) -> None:
        super().__init__(ServiceRtc.kServiceType)
        self._channel_name = channel_name.encode("utf-8")
        if uid == 0 or uid == "0":
            self._uid = b""
        else:
            self._uid = str(uid).encode("utf-8")

    def pack(self) -> bytes:
        return super().pack() + pack_string(self._channel_name) + pack_string(self._uid)


class ServiceRtm(Service):
    kServiceType = 2
    kPrivilegeLogin = 1

    def __init__(self, user_id: str | int = "") -> None:
        super().__init__(ServiceRtm.kServiceType)
        if user_id == 0 or user_id == "0":
            self._user_id = b""
        else:
            self._user_id = str(user_id).encode("utf-8")

    def pack(self) -> bytes:
        return super().pack() + pack_string(self._user_id)


class ServiceConvoAI(Service):
    """Agora Conversational AI Engine service (Service Type 9)."""
    kServiceType = 9

    def __init__(self) -> None:
        super().__init__(ServiceConvoAI.kServiceType)

    def pack(self) -> bytes:
        return super().pack()


# --- AccessToken2 Core ---


class AccessToken:
    def __init__(
        self,
        app_id: str = "",
        app_certificate: str = "",
        issue_ts: int = 0,
        expire: int = 900,
    ) -> None:
        self._app_id = app_id
        self._app_cert = app_certificate
        self._issue_ts = issue_ts if issue_ts != 0 else int(time.time())
        self._expire = expire
        self._salt = secrets.SystemRandom().randint(1, 99999999)
        self.services: list[Service] = []

    def _signing(self, app_certificate: bytes) -> bytes:
        signing = hmac.new(
            pack_uint32(self._issue_ts), app_certificate, hashlib.sha256
        ).digest()
        signing = hmac.new(
            pack_uint32(self._salt), signing, hashlib.sha256
        ).digest()
        return signing

    def _is_uuid(self, data: str) -> bool:
        if len(data) != 32:
            return False
        try:
            bytes.fromhex(data)
            return True
        except (ValueError, TypeError):
            return False

    def add_service(self, service: Service) -> None:
        self.services.append(service)

    def build(self) -> str:
        if not self._is_uuid(self._app_id) or not self._is_uuid(self._app_cert):
            return ""
        if not self.services:
            return ""

        app_id_bytes = self._app_id.encode("utf-8")
        app_cert_bytes = self._app_cert.encode("utf-8")
        signing = self._signing(app_cert_bytes)

        services = sorted(self.services, key=lambda s: s.service_type())
        signing_info = (
            pack_string(app_id_bytes)
            + pack_uint32(self._issue_ts)
            + pack_uint32(self._expire)
            + pack_uint32(self._salt)
            + pack_uint16(len(services))
        )
        for service in services:
            signing_info += service.pack()

        signature = hmac.new(signing, signing_info, hashlib.sha256).digest()
        compressed = zlib.compress(pack_string(signature) + signing_info)
        return get_version() + base64.b64encode(compressed).decode("utf-8")


class RtcTokenBuilder:
    Role_Publisher = 1
    Role_Subscriber = 2

    @staticmethod
    def build_token_with_uid(
        app_id: str,
        app_certificate: str,
        channel_name: str,
        uid: int | str,
        role: int = 1,
        token_expire: int = 3600,
        privilege_expire: int = 3600,
    ) -> str:
        token = AccessToken(app_id, app_certificate, expire=token_expire)
        service_rtc = ServiceRtc(channel_name, uid)
        service_rtc.add_privilege(ServiceRtc.kPrivilegeJoinChannel, privilege_expire)
        if role == RtcTokenBuilder.Role_Publisher:
            service_rtc.add_privilege(ServiceRtc.kPrivilegePublishAudioStream, privilege_expire)
            service_rtc.add_privilege(ServiceRtc.kPrivilegePublishVideoStream, privilege_expire)
            service_rtc.add_privilege(ServiceRtc.kPrivilegePublishDataStream, privilege_expire)
        token.add_service(service_rtc)
        return token.build()


def generate_rtc_token(
    app_id: str,
    app_certificate: str,
    channel_name: str,
    uid: int | str,
    expire_seconds: int = 3600,
    role: int = RtcTokenBuilder.Role_Publisher,
) -> str:
    """Generate an official Agora AccessToken2 (v007) RTC token.

    Falls back to mock token only if app_id or app_certificate are missing or not 32-char hex.
    """
    if not app_id or not app_certificate:
        return f"mock-token-{channel_name}-{uid}"

    token = RtcTokenBuilder.build_token_with_uid(
        app_id=app_id.strip(),
        app_certificate=app_certificate.strip(),
        channel_name=channel_name,
        uid=uid,
        role=role,
        token_expire=expire_seconds,
        privilege_expire=expire_seconds,
    )
    if not token:
        # If credentials are not valid 32-hex UUIDs (e.g. test dummy values), return a mock token
        return f"mock-token-{channel_name}-{uid}"

    return token


class ConvoAITokenBuilder:
    """Builds official Agora AccessToken2 (v007) containing RTC, RTM, and ConvoAI services."""

    @staticmethod
    def build_token(
        app_id: str,
        app_certificate: str,
        channel_name: str,
        account: int | str,
        role: int = RtcTokenBuilder.Role_Publisher,
        token_expire: int = 3600,
        privilege_expire: int = 3600,
    ) -> str:
        token = AccessToken(app_id, app_certificate, expire=token_expire)

        # 1. RTC Service (audio/video/datastream permissions)
        service_rtc = ServiceRtc(channel_name, account)
        service_rtc.add_privilege(ServiceRtc.kPrivilegeJoinChannel, privilege_expire)
        if role == RtcTokenBuilder.Role_Publisher:
            service_rtc.add_privilege(ServiceRtc.kPrivilegePublishAudioStream, privilege_expire)
            service_rtc.add_privilege(ServiceRtc.kPrivilegePublishVideoStream, privilege_expire)
            service_rtc.add_privilege(ServiceRtc.kPrivilegePublishDataStream, privilege_expire)
        token.add_service(service_rtc)

        # 2. RTM Service (messaging / signaling)
        service_rtm = ServiceRtm(account)
        service_rtm.add_privilege(ServiceRtm.kPrivilegeLogin, privilege_expire)
        token.add_service(service_rtm)

        # 3. ConvoAI Service (service type 9)
        token.add_service(ServiceConvoAI())

        return token.build()


def generate_convoai_token(
    app_id: str,
    app_certificate: str,
    channel_name: str,
    uid: int | str,
    expire_seconds: int = 3600,
    role: int = RtcTokenBuilder.Role_Publisher,
) -> str:
    """Generate an official combined RTC + RTM + ConvoAI token (v007).

    Falls back to mock token only if app_id or app_certificate are missing or not 32-char hex.
    """
    if not app_id or not app_certificate:
        return f"mock-convoai-token-{channel_name}-{uid}"

    token = ConvoAITokenBuilder.build_token(
        app_id=app_id.strip(),
        app_certificate=app_certificate.strip(),
        channel_name=channel_name,
        account=uid,
        role=role,
        token_expire=expire_seconds,
        privilege_expire=expire_seconds,
    )
    if not token:
        return f"mock-convoai-token-{channel_name}-{uid}"

    return token
