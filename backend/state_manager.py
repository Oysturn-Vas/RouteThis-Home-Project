from typing import Optional, Dict, Any, List
import boto3
from botocore.exceptions import ClientError
import json
import time
import logging
from config import settings

logger = logging.getLogger(__name__)

class DynamoDBSessionManager:
    def __init__(self, table_name=settings.DYNAMODB_SESSIONS_TABLE, region_name=settings.AWS_REGION):
        self.table_name = table_name
        try:
            self.dynamodb = boto3.resource(
                'dynamodb',
                region_name=region_name,
                aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
                aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY
            )
            self.table = self.dynamodb.Table(self.table_name)
        except Exception as e:
            logger.exception(f"Failed to initialize DynamoDB resource: {e}")
            raise

    def save_session(self, session_id: str, chat_history: List[Dict[str, Any]], troubleshooting_step: str):
        ttl = int(time.time()) + 86400  # 24 hours from now
        try:
            self.table.put_item(
                Item={
                    'session_id': session_id,
                    'chat_history': json.dumps(chat_history),
                    'troubleshooting_step': troubleshooting_step,
                    'last_updated': int(time.time()),
                    'expiration_time': ttl
                }
            )
            logger.info(f"Session state saved for session_id: {session_id}")
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            logger.exception(
                f"DynamoDB Error ({error_code}) saving session {session_id}: {e}"
            )
        except Exception as e:
            logger.exception(f"An unexpected error occurred while saving session {session_id}: {e}")

    def load_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        try:
            response = self.table.get_item(Key={'session_id': session_id})
            if 'Item' in response:
                item = response['Item']
                item['chat_history'] = json.loads(item['chat_history'])
                logger.info(f"Session loaded successfully for session_id: {session_id}")
                return item
            else:
                logger.warning(f"Session not found for session_id: {session_id}. This is expected for new conversations.")
                return None
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            logger.exception(
                f"DynamoDB Error ({error_code}) loading session {session_id}: {e}"
            )
            return None
        except Exception as e:
            logger.exception(f"An unexpected error occurred while loading session {session_id}: {e}")
            return None


class StateManager:
    def __init__(self):
        pass

    def get_system_prompt(self, image_context: Optional[str] = None) -> str:
        base_prompt = """You are RouteMaster AI, a friendly and conversational technical support agent for RouteThis.
Your goal is to help users with their Linksys EA6350 router.

General Guidelines:
- Be conversational, helpful, and answer user questions about their router or the troubleshooting process.
- If the user asks about topics not related to their WiFi or router, gently guide the conversation back to the router issue.
- You are specifically trained on the Linksys EA6350. If the user asks about a different model, explain that you can only provide support for the Linksys EA6350. Avoid giving generic networking advice.
- When you need to provide technical instructions, use the `query_manual` tool to get accurate steps for the Linksys EA6350.
- When you get instructions from the manual, deliver them clearly. It's often best to provide them one step at a time, waiting for the user to confirm they've completed each step before moving on.
- While waiting for search results from the manual, you can use phrases like "Let me look that up for you..." or "One moment while I check the manual."

IMPORTANT: Do NOT invent, assume, or ask about personal details such as family members, names, locations, or any information not explicitly provided by the user. Only respond based on what the user has actually stated. If you are unsure about something the user said, ask for clarification.

AUDIO HANDLING: If you receive a message that was difficult to hear clearly (indicated by text in brackets like "[User had trouble being heard clearly...]"), you should acknowledge this politely and suggest that the user type their message instead if they continue to have trouble being understood. You can say something like: "I'm having some trouble hearing you clearly. If you'd like, you can type your message in the text box instead - that might work better for us to understand each other."

Troubleshooting Flow:
We generally follow these steps to resolve issues:

1.  **Understand the Problem (Qualification):**
    *   Start by asking questions to understand the user's issue (e.g., slow speeds, no connection, red lights).
    *   Based on their problem, determine if a router reboot is a good next step.
    *   If a reboot seems appropriate, ask for their permission to guide them through it.
    *   If a reboot isn't the right solution (e.g., they need a password reset), explain why and offer the correct guidance.

2.  **Guide the Reboot (Instructions):**
    *   If the user agrees to a reboot, you must provide all the reboot steps at once.
    *   Use `query_manual` to get the correct steps.
    *   After providing the steps, tell the user that you will be waiting for them to reconnect after the reboot is complete.

3.  **Check the Result (Verification):**
    *   When the user reconnects, welcome them back and ask if the problem is solved.

4.  **Wrap Up (Resolution & Exit):**
    *   If the issue is fixed, that's great! End the conversation with a friendly sign-off.
    *   If the issue is still there, apologize that the reboot didn't work and suggest they contact their Internet Service Provider or human support for more help.
    *   IMPORTANT: When ending a conversation, include the exact marker [END_SESSION] at the very end of your final response. This marker signals the system to disconnect. Do not include any text after this marker.

CRITICAL SAFETY GUARDRAIL: If the user mentions 'smoke', 'fire', 'burning', or 'sparks', tell them to unplug the router immediately, contact emergency services if necessary, and terminate the call immediately."""
        
        if image_context:
            base_prompt += f"\n\n[User Uploaded Image Context]: {image_context}\n(Use this image context to inform your answers, but do not change the 4-phase process.)\n"

        return base_prompt
