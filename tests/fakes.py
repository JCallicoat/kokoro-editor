"""Stand-ins for Qt's network classes, recording requests and letting
tests play the server's part."""

from __future__ import annotations

import json

from PySide6.QtNetwork import QNetworkReply


class FakeSignal:
    def __init__(self) -> None:
        self.slots = []

    def connect(self, slot) -> None:
        self.slots.append(slot)

    def emit(self) -> None:
        for slot in self.slots:
            slot()


class FakeReply:
    def __init__(self) -> None:
        self.readyRead = FakeSignal()
        self.finished = FakeSignal()
        self.status = 200
        self.data = b""
        self.network_error = QNetworkReply.NetworkError.NoError
        self.aborted = False

    def error(self):
        return self.network_error

    def errorString(self) -> str:
        return "Connection refused"

    def attribute(self, _attribute):
        return self.status

    def readAll(self):
        data, self.data = self.data, b""
        return type("Bytes", (), {"data": lambda _self: data})()

    def abort(self) -> None:
        self.aborted = True
        self.finished.emit()  # Qt reports aborted replies as finished

    def deleteLater(self) -> None:
        pass


class FakeNam:
    def __init__(self) -> None:
        self.posts: list[dict] = []
        self.replies: list[FakeReply] = []

    def post(self, _request, body):
        self.posts.append(json.loads(bytes(body.data())))
        self.replies.append(FakeReply())
        return self.replies[-1]
