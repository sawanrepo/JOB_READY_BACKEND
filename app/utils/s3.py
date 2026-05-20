import boto3
import logging
import asyncio
from app.config import settings

logger = logging.getLogger(__name__)

def get_s3_client():
    if not settings.AWS_ACCESS_KEY_ID or not settings.AWS_SECRET_ACCESS_KEY:
        logger.warning("AWS credentials not configured. S3 operations will fail.")
        return None
    try:
        return boto3.client(
            's3',
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            region_name=settings.AWS_REGION
        )
    except Exception as e:
        logger.error(f"Failed to initialize S3 client: {e}")
        return None

async def upload_file_to_s3(local_path: str, s3_key: str) -> bool:
    s3_client = get_s3_client()
    if not s3_client:
        return False
    if not settings.AWS_S3_BUCKET_NAME:
        logger.error("AWS_S3_BUCKET_NAME is not set.")
        return False
    try:
        await asyncio.to_thread(
            s3_client.upload_file,
            local_path,
            settings.AWS_S3_BUCKET_NAME,
            s3_key
        )
        logger.info(f"Successfully uploaded {local_path} to S3 as {s3_key}")
        return True
    except Exception as e:
        logger.error(f"S3 upload failed for key {s3_key}: {e}")
        return False

async def get_file_from_s3(s3_key: str):
    s3_client = get_s3_client()
    if not s3_client:
        return None
    if not settings.AWS_S3_BUCKET_NAME:
        return None
    try:
        response = await asyncio.to_thread(
            s3_client.get_object,
            Bucket=settings.AWS_S3_BUCKET_NAME,
            Key=s3_key
        )
        return response
    except Exception as e:
        logger.error(f"Failed to fetch {s3_key} from S3: {e}")
        return None

async def delete_file_from_s3(s3_key: str) -> bool:
    s3_client = get_s3_client()
    if not s3_client:
        return False
    if not settings.AWS_S3_BUCKET_NAME:
        return False
    try:
        await asyncio.to_thread(
            s3_client.delete_object,
            Bucket=settings.AWS_S3_BUCKET_NAME,
            Key=s3_key
        )
        logger.info(f"Successfully deleted {s3_key} from S3")
        return True
    except Exception as e:
        logger.error(f"Failed to delete {s3_key} from S3: {e}")
        return False

async def generate_presigned_url(s3_key: str, expiration=3600) -> str:
    s3_client = get_s3_client()
    if not s3_client:
        return ""
    if not settings.AWS_S3_BUCKET_NAME:
        return ""
    try:
        url = await asyncio.to_thread(
            s3_client.generate_presigned_url,
            'get_object',
            Params={
                'Bucket': settings.AWS_S3_BUCKET_NAME,
                'Key': s3_key
            },
            ExpiresIn=expiration
        )
        return url
    except Exception as e:
        logger.error(f"Failed to generate presigned URL for {s3_key}: {e}")
        return ""
