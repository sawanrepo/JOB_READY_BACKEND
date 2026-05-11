from datetime import datetime, timezone
from app.models.user import User

def can_use_feature(user: User, feature: str) -> bool:
    mapping = {
        "ats_check": (user.ats_checks_left_today or 0) + (user.purchased_ats_credits or 0),
        "resume_tailoring": (user.resume_tailoring_left_this_week or 0) + (user.purchased_tailor_credits or 0),
        "mock_interview": user.mock_interviews_left_this_month or 0,
        "audio_interview": user.audio_interviews_left_this_month or 0
    }
    return mapping.get(feature, 0) > 0


def deduct_feature_usage(user: User, feature: str):
    now = datetime.now(timezone.utc)
    
    if feature == "ats_check":
        if user.ats_checks_left_today > 0:
            user.ats_checks_left_today -= 1
        else:
            user.purchased_ats_credits -= 1
        user.last_ats_check_at = now

    elif feature == "resume_tailoring":
        if user.resume_tailoring_left_this_week > 0:
            user.resume_tailoring_left_this_week -= 1
        else:
            user.purchased_tailor_credits -= 1
        user.last_resume_tailoring_at = now

    elif feature == "mock_interview":
        user.mock_interviews_left_this_month -= 1
        user.last_mock_interview_at = now

    elif feature == "audio_interview":
        if user.audio_interviews_left_this_month is None:
            user.audio_interviews_left_this_month = 0
        user.audio_interviews_left_this_month -= 1
        # Re-use last_mock_interview_at or ignore since it's one-time only anyway