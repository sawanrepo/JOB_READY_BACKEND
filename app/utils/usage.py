from datetime import datetime, timezone
from app.models.user import User

def can_use_feature(user: User, feature: str) -> bool:
    mapping = {
        "ats_check": user.ats_checks_left_today,
        "resume_tailoring": user.resume_tailoring_left_this_week,
        "mock_interview": user.mock_interviews_left_this_month
    }
    return mapping.get(feature, 0) > 0


def deduct_feature_usage(user: User, feature: str):
    now = datetime.now(timezone.utc)
    
    if feature == "ats_check":
        user.ats_checks_left_today -= 1
        user.last_ats_check_at = now

    elif feature == "resume_tailoring":
        user.resume_tailoring_left_this_week -= 1
        user.last_resume_tailoring_at = now

    elif feature == "mock_interview":
        user.mock_interviews_left_this_month -= 1
        user.last_mock_interview_at = now