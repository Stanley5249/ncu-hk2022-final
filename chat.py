from __future__ import annotations

from abc import abstractmethod
from collections import defaultdict, Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import partial
from os import PathLike
from pathlib import Path
import string
from typing import Any, Protocol, TypedDict, TypeGuard
from urllib.parse import parse_qs, quote, urljoin

import yaml
from linebot.v3.messaging import (
    FlexContainer,
    FlexMessage,
    Message,
    TextMessage,
    ImageMessage,
)

__all__ = ["Chatflow"]


class RawChat(TypedDict):
    messages: list[RawMessage]
    action: RawAction


class RawMessage(TypedDict):
    type: str
    data: Any


class RawAction(TypedDict):
    type: str


class ChatLike(Protocol):
    @abstractmethod
    def get_messages(self, url: str = "") -> Sequence[Message]: ...

    @abstractmethod
    def transition(self, text: str) -> ChatLike: ...


class ChatMaker(Protocol):
    def __call__(self, state: Counter[str]) -> ChatLike: ...


@dataclass
class ChatNext(ChatLike):
    messages: list[Message]
    dest: str
    state: Counter[str]

    def get_messages(self, url: str = "") -> list[Message]:
        return [
            ImageMessage(
                quickReply=None,
                originalContentUrl=urljoin(url, m.original_content_url),
                previewImageUrl=urljoin(url, m.preview_image_url),
            )
            if isinstance(m, ImageMessage)
            else m
            for m in self.messages
        ]

    def transition(self, text: str) -> ChatLike:
        return CHATFLOW_DATA[self.dest](self.state)


UNKNOWN_ERROR = TextMessage(
    quickReply=None,
    text="很抱歉，我不太懂你說的。",
    quoteToken=None,
)
QUESTION_MISMATCH = TextMessage(
    quickReply=None,
    text="你似乎看錯題目了。",
    quoteToken=None,
)
WRONG_ANSWER = TextMessage(
    quickReply=None,
    text="再試試看吧！",
    quoteToken=None,
)
DUPLICATE_ANSWER = TextMessage(
    quickReply=None,
    text="你已經選過了喔。",
    quoteToken=None,
)


@dataclass
class ChatQA(ChatLike):
    messages: Sequence[Message]
    dest: str
    label: str
    answer: str
    state: Counter[str]
    attempt: set[str] = field(default_factory=set)

    def get_messages(self, url: str = "") -> Sequence[Message]:
        return self.messages

    def transition(self, text: str) -> ChatLike:
        qa = parse_qs(text)

        match qa:
            case {"q": [q], "a": [a]}:
                pass
            case _:
                self.messages = [UNKNOWN_ERROR]
                return self

        if q != self.label:
            self.messages = [QUESTION_MISMATCH]
            return self

        if a in self.attempt:
            self.messages = [DUPLICATE_ANSWER]
            return self

        if a != self.answer:
            self.attempt.add(a)
            self.messages = [WRONG_ANSWER]
            return self

        return CHATFLOW_DATA[self.dest](self.state)


@dataclass
class ChatStore(ChatLike):
    messages: Sequence[Message]
    dest: str
    label: str
    state: Counter[str]

    def get_messages(self, url: str = "") -> Sequence[Message]:
        return self.messages

    def transition(self, text: str) -> ChatLike:
        qa = parse_qs(text)

        match qa:
            case {"q": [q], "a": [a]}:
                pass
            case _:
                self.messages = [UNKNOWN_ERROR]
                return self

        if q != self.label:
            self.messages = [QUESTION_MISMATCH]
            return self

        self.state[a] += 1
        return CHATFLOW_DATA[self.dest](self.state)


@dataclass
class ChatEnd(ChatLike):
    messages: Sequence[Message]
    dest: str
    state: Counter[str]

    def get_messages(self, url: str = "") -> Sequence[Message]:
        ((k, v),) = self.state.most_common(1)
        i = ord(k) - ord("A")
        return [self.messages[i]]

    def transition(self, text: str) -> ChatLike:
        return CHATFLOW_DATA[self.dest](Counter())


@dataclass
class ChatInit(ChatLike):
    def get_messages(self, url: str = "") -> list[Message]:
        return []

    def transition(self, text: str) -> ChatLike:
        return CHATFLOW_DATA["Begin"](Counter())


class Chatflow(defaultdict[str, ChatLike]):
    def __missing__(self, key: str) -> ChatLike:
        return ChatInit()


def load_chatflow(path: str | PathLike[str]) -> dict[str, ChatMaker]:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    assert validate_chatflow(raw)
    return parse_chatflow(raw)


def validate_chatflow(raw: Any) -> TypeGuard[dict[str, RawChat]]:
    if not isinstance(raw, dict):
        raise ValueError("input object must be a dict")

    for k, v in raw.items():
        if not isinstance(k, str):
            raise ValueError(f"key {k} must be a str")

        match v:
            case {"messages": [*messages], "action": action}:
                pass
            case _:
                raise ValueError(f"{v} at {k} must have 'messages' and 'action' keys")

        for m in messages:
            match m:
                case {"type": str()}:
                    pass
                case _:
                    raise ValueError(f"invalid message {m} at {k}")

        match action:
            case {"type": str()}:
                pass
            case _:
                raise ValueError(f"invalid action {action} at {k}")
    return True


def parse_chatflow(raw: dict[str, RawChat]) -> dict[str, ChatMaker]:
    return {key: parse_chat(val) for key, val in raw.items()}


def parse_chat(raw: RawChat) -> ChatMaker:
    messages = [parse_message(m) for m in raw["messages"]]
    match raw["action"]:
        case {"type": "next", "dest": str(dest)}:
            return partial(ChatNext, messages, dest)
        case {"type": "qa", "dest": str(dest), "label": str(label), "answer": str(a)}:
            return partial(ChatQA, messages, dest, label, a)
        case {"type": "store", "dest": str(dest), "label": str(label)}:
            return partial(ChatStore, messages, dest, label)
        case {"type": "end", "dest": str(dest)}:
            return partial(ChatEnd, messages, dest)
    raise ValueError(f"invalid action type, {raw['action']}")


def parse_message(raw: Any) -> Message:
    match raw:
        case {"type": "text", "data": str(data)}:
            return TextMessage(
                quickReply=None,
                text=data,
                quoteToken=None,
            )
        case {
            "type": "image",
            "original": str(original),
            "preview": str(preview),
        }:
            return ImageMessage(
                quickReply=None,
                originalContentUrl=quote(original),
                previewImageUrl=quote(preview),
            )
        case {"type": "flex", "data": {**data}}:
            return FlexMessage(
                quickReply=None,
                altText="flex",
                contents=FlexContainer.from_dict(data),
            )
        case {
            "type": "template",
            "data": {
                "id": int(id),
                "label": str(label),
                "title": str(title),
                "options": [*options],
                "fg": str(fg),
                "bg": str(bg),
            },
        } if all(isinstance(opt, str) for opt in options):
            if id == 1:
                data = from_template_1(label, title, options, fg, bg)
            elif id == 2:
                data = from_template_2(label, title, options, fg, bg)
            else:
                raise ValueError(f"invalid template id, {id}")
            return FlexMessage(
                quickReply=None,
                altText="flex",
                contents=FlexContainer.from_dict(data),
            )
    raise ValueError(f"invalid message type, {raw}")


def from_template_1(
    label: str,
    title: str,
    options: Sequence[str],
    fg: str,
    bg: str,
) -> dict[str, Any]:
    return {
        "body": {
            "contents": [{"text": title, "type": "text", "wrap": True}],
            "layout": "vertical",
            "type": "box",
        },
        "footer": {
            "contents": [
                {"color": fg, "type": "separator"},
                *(
                    {
                        "action": {
                            "data": f"q={label}&a={a}",
                            "displayText": f"我選{q}！",
                            "label": q,
                            "type": "postback",
                        },
                        "color": fg,
                        "type": "button",
                    }
                    for q, a in zip(options, string.ascii_uppercase)
                ),
                {"color": fg, "type": "separator"},
            ],
            "layout": "vertical",
            "spacing": "sm",
            "type": "box",
        },
        "styles": {"body": {"backgroundColor": bg}, "footer": {"backgroundColor": bg}},
        "type": "bubble",
    }


def from_template_2(
    label: str,
    title: str,
    options: Sequence[str],
    fg: str,
    bg: str,
) -> dict[str, Any]:
    return {
        "type": "carousel",
        "contents": [
            {
                "type": "bubble",
                "size": "nano",
                "body": {
                    "type": "box",
                    "layout": "vertical",
                    "contents": [
                        {"type": "text", "text": title, "wrap": True, "size": "sm"}
                    ],
                },
                "styles": {"body": {"backgroundColor": bg}},
            },
            *(
                {
                    "type": "bubble",
                    "size": "nano",
                    "body": {
                        "type": "box",
                        "layout": "vertical",
                        "contents": [{"type": "text", "text": opt, "wrap": True}],
                    },
                    "footer": {
                        "type": "box",
                        "layout": "vertical",
                        "contents": [
                            {"type": "separator", "color": fg},
                            {
                                "type": "button",
                                "action": {
                                    "type": "postback",
                                    "label": a,
                                    "data": f"q={label}&a={a}",
                                    "displayText": f"我選{a}！",
                                },
                                "color": fg,
                            },
                        ],
                    },
                    "styles": {
                        "body": {"backgroundColor": bg},
                        "footer": {"backgroundColor": bg},
                    },
                }
                for opt, a in zip(options, string.ascii_uppercase)
            ),
        ],
    }


CHATFLOW_DATA = load_chatflow(Path("resource/chatflow.yaml"))
