from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class UserCreate(BaseModel):
    name: str
    line_id: Optional[str] = None
    face_label: str
    photo_url: Optional[str] = None


class DetailCreate(BaseModel):
    age: Optional[int] = None
    gender: Optional[str] = None
    addres: Optional[str] = None


class FaceFrameResponse(BaseModel):
    identified: bool
    name: Optional[str] = None
    distance: Optional[float] = None
    face_count: int
    error: Optional[str] = None


class EmotionFrameResult(BaseModel):
    detected: bool
    emotion_type: Optional[str] = None
    emotion_score: Optional[float] = None
    probabilities: dict
    error: Optional[str] = None


class EmotionSave(BaseModel):
    emotion_type: str
    emotion_score: float
    note: Optional[str] = None


class OCRResult(BaseModel):
    med_name: str
    dosage: Optional[str] = None
    quantity: Optional[str] = None
    pill_count: Optional[int] = None
    amount_each_intake: Optional[str] = None
    total_intake: Optional[str] = None
    schedule_time: Optional[dict] = None
    instructions: Optional[str] = None
    warning: Optional[str] = None
    intake_time_label: Optional[str] = None
    clinical_uses: Optional[str] = None
    manufacturer: Optional[str] = None
    hospital: Optional[str] = None
    prescription_no: Optional[str] = None
    use_before: Optional[str] = None
    physician: Optional[str] = None
    pharmacist: Optional[str] = None
    patient_name: Optional[str] = None
    date_dispensed: Optional[str] = None
    pill_description: Optional[str] = None
    error: Optional[str] = None


class MedicationCreate(BaseModel):
    med_name: str
    dosage: Optional[str] = None
    pill_prescribed: int
    pills_remaining: Optional[int] = None
    schedule_time: Optional[dict] = None
    instructions: Optional[str] = None
    warning: Optional[str] = None
    amount_each_intake: Optional[str] = None
    pill_description: Optional[str] = None
    prescription_meta: Optional[dict] = None


class IntakeSave(BaseModel):
    med_id: int
    emot_id: Optional[int] = None
    intake_stats: str = "taken"


class TodayIntakeItem(BaseModel):
    intk_id: Optional[int] = None
    med_id: int
    med_name: str
    scheduled_slots: list
    intake_stats: str
    pill_prescribed: int
    total_intake: int
