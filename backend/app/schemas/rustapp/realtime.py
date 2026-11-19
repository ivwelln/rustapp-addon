from pydantic import Field, BaseModel, SecretStr


class RustAppHandshake(BaseModel):
    centrifugo_token: SecretStr = Field(min_length=1)
