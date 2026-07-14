import os
import json
import logging
from google import genai
from google.genai import types

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gemini_service")

# Initialize Gemini Client if API key is provided
api_key = os.getenv("GEMINI_API_KEY")
client = None

if api_key:
    try:
        client = genai.Client(api_key=api_key)
        logger.info("Gemini API client initialized successfully.")
    except Exception as e:
        logger.error(f"Failed to initialize Gemini Client: {e}")
else:
    logger.warning("GEMINI_API_KEY not found in environment. Gemini Service running in MOCK fallback mode.")

def classify_error_to_tag(error_log: str, available_tags: list[str]) -> str:
    """
    Analyzes the seeker's error log and matches it to the best tag.
    Falls back to case-insensitive keyword parsing if Gemini is offline or mock mode is active.
    """
    if not available_tags:
        return "General"
        
    if not client:
        # Fallback simple keyword match
        error_lower = error_log.lower()
        for tag in available_tags:
            if tag.lower() in error_lower:
                return tag
        return "General" if "General" in available_tags else available_tags[0]

    prompt = f"""
    You are an expert developer routing assistant. Analyze the developer error log/description:
    
    ---
    {error_log}
    ---

    From this list of tags: {', '.join(available_tags)}, choose the single best matching skill tag.
    Respond with ONLY the exact tag name from the list. Do not include quotes, periods, or extra words.
    If none of the specific tags fit the error description, respond with "General" (if it is in the list) or the closest matching tag.
    """
    
    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
        )
        tag_result = response.text.strip()
        # Verify the returned string matches one of our tags
        for tag in available_tags:
            if tag_result.lower() == tag.lower() or tag.lower() in tag_result.lower():
                return tag
        return tag_result
    except Exception as e:
        logger.error(f"Gemini API tag classification failed: {e}. Falling back to keyword search.")
        # Fallback
        error_lower = error_log.lower()
        for tag in available_tags:
            if tag.lower() in error_lower:
                return tag
        return "General" if "General" in available_tags else available_tags[0]

def generate_bug_fix_recipe(seeker_username: str, helper_username: str, error_log: str, chat_history: str) -> dict:
    """
    Summarizes a debugging chat transcript and error log into a markdown Bug-Fix Recipe.
    Returns a dictionary with keys: 'title', 'problem', and 'solution'.
    """
    if not client:
        # Mock fallback recipe
        return {
            "title": f"Debugging {seeker_username}'s issue with {helper_username}",
            "problem": f"### Error Log:\n```\n{error_log}\n```\n\nSeeker `{seeker_username}` encountered this error log during development.",
            "solution": f"### Solution Walkthrough:\n- Helper `{helper_username}` joined the session.\n- Together, they analyzed the stack trace, validated configuration, and resolved the issue.\n\n*Check the live chat logs for raw chat details.*"
        }

    prompt = f"""
    You are an expert technical editor. You are given a developer's error log and the live chat transcript of the session where a seeker ({seeker_username}) and a helper ({helper_username}) resolved it.
    
    Seeker's original error log:
    {error_log}
    
    Chat Transcript of the debugging session:
    {chat_history}
    
    Extract the key troubleshooting insights and generate a clean JSON output with these keys:
    1. "title": A concise, search-optimized title describing the bug and its fix (e.g. "Fixing circular import in FastAPI models").
    2. "problem": A clean Markdown description of what the error was and why it occurred.
    3. "solution": A structured Markdown step-by-step description of the solution, including code snippets and explanations.
    
    Ensure you return ONLY the JSON object. Do not include markdown code fence formatting (like ```json) in your outer response.
    """
    
    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
            ),
        )
        recipe_data = json.loads(response.text.strip())
        return {
            "title": recipe_data.get("title", f"Bug fix by {seeker_username} & {helper_username}"),
            "problem": recipe_data.get("problem", error_log),
            "solution": recipe_data.get("solution", "The issue was debugged and resolved by helper.")
        }
    except Exception as e:
        logger.error(f"Gemini API recipe generation failed: {e}. Falling back to default format.")
        return {
            "title": f"Fix for session by {seeker_username} and {helper_username}",
            "problem": f"Seeker {seeker_username} reported an error:\n\n```\n{error_log}\n```",
            "solution": "The issue was discussed and resolved during the live debugging session. Refer to chat history for details."
        }
