from pydantic import BaseModel, Field

class UserCreate(BaseModel):
    username: str
    level: str = "CET4"
    target_score: int = 425

class WordReview(BaseModel):
    user_id: str
    word: str
    remembered: bool

class WrongReview(BaseModel):
    user_id: str
    question_id: str
    deep_mode: bool = False

class ContentGenRequest(BaseModel):
    prompt: str
    content_type: str = "image"  # image / video

class TranslateRequest(BaseModel):
    text: str
    source_lang: str = "zh"
    target_lang: str = "en"

class ASRRequest(BaseModel):
    audio_base64: str
    language: str = "en"