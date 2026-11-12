from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator

Version = Annotated[StrictInt, Field(ge=0)]


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(InputModel):
    username: Annotated[StrictStr, Field(min_length=1, max_length=64)]
    password: Annotated[StrictStr, Field(min_length=1, max_length=1024)]


class Change(InputModel):
    key: Annotated[StrictStr, Field(min_length=1, max_length=256)]
    value: StrictStr | None
    expected_version: Version

    @field_validator("key")
    @classmethod
    def validate_key(cls, key):
        reserved = ("litegame.sync", "litegame.server", "litegame.auth", "litegame.connection", "litegame.device")
        if (not key.startswith(("litegame.", "chogan."))
                or key in ("litegame.", "chogan.")
                or any(key == prefix or key.startswith(prefix + ".") for prefix in reserved)
                or any(ord(c) < 32 or ord(c) == 127 for c in key)):
            raise ValueError("Only game storage keys are allowed; connection/authentication keys are reserved")
        return key


class Batch(InputModel):
    changes: Annotated[list[Change], Field(min_length=1, max_length=100)]

    @field_validator("changes")
    @classmethod
    def unique_keys(cls, changes):
        if len({change.key for change in changes}) != len(changes):
            raise ValueError("Duplicate keys in one batch")
        return changes


class Snapshot(BaseModel):
    model_config = ConfigDict(extra="allow")
    format: Literal[1]
    emulator: Literal["jsnes-2.1.0"]
    sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    title: Annotated[StrictStr, Field(max_length=512)]
    savedAt: Annotated[StrictStr, Field(max_length=64)]
    state: dict

    @field_validator("state")
    @classmethod
    def complete_state(cls, state):
        if not all(isinstance(state.get(key), dict) for key in ("cpu", "ppu", "mmap", "papu")):
            raise ValueError("Incomplete JSNES snapshot")
        return state


class NesWrite(InputModel):
    expected_version: Version
    snapshot: Snapshot
