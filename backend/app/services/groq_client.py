from groq import Groq

from app.config import get_settings


def groq_completion(system_prompt: str, user_prompt: str) -> str | None:
    settings = get_settings()
    if not settings.groq_api_key:
        return None

    client = Groq(api_key=settings.groq_api_key)
    completion = client.chat.completions.create(
        model=settings.groq_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.4,
    )
    return completion.choices[0].message.content
