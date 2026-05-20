from sqlalchemy import Column, Integer, String, DateTime
from sqlalchemy.sql import func
from app.database import Base

class ProcessedPayment(Base):
    __tablename__ = "processed_payments"
    
    id = Column(Integer, primary_key=True, index=True)
    razorpay_payment_id = Column(String, unique=True, index=True, nullable=False)
    razorpay_order_id = Column(String, unique=True, index=True, nullable=False)
    user_id = Column(Integer, nullable=False)
    amount = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
