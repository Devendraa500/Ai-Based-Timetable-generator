import os
from openai import OpenAI
import google.generativeai as genai

def generate_response(prompt):
    OPENAI_KEY = os.getenv("OPENAI_API_KEY")
    GEMINI_KEY = os.getenv("GEMINI_API_KEY")

    # -------- OPENAI --------
    if OPENAI_KEY:
        try:
            client = OpenAI(api_key=OPENAI_KEY)

            response = client.chat.completions.create(
                model="gpt-4o-mini",  
                messages=[
                    {"role": "system", "content": "You are a helpful academic assistant."},
                    {"role": "user", "content": prompt}
                ]
            )

            return response.choices[0].message.content

        except Exception as e:
            print("OpenAI Error:", e)

    # -------- GEMINI --------
    if GEMINI_KEY:
        try:
            genai.configure(api_key=GEMINI_KEY)
            model = genai.GenerativeModel("gemini-3-flash-preview")
            res = model.generate_content(prompt)
            return res.text

        except Exception as e:
            print("Gemini Error:", e)

    return "LLM not configured."
