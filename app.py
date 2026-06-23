import os
import json
import hashlib
import datetime
import threading
import queue
import asyncio
import re
import base64
import tempfile
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, asdict
import sqlite3
import pickle

# AI Libraries
import google.generativeai as genai
import openai
import speech_recognition as sr
import pyttsx3
import whisper
from gtts import gTTS
from elevenlabs import generate, play, clone, VoiceSettings
import torch
from transformers import pipeline, AutoTokenizer, AutoModelForCausalLM

# Web & Browser
import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

# UI / Server
import gradio as gr
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
import uvicorn

# Database
import pymongo
import redis

# ============================================
# CONFIGURATION - YOUR AVATAR SETTINGS
# ============================================

CONFIG = {
    "avatar_name": "Your AI Avatar",
    "creator": "Your Name",
    "gemini_api_key": "YOUR_GOOGLE_API_KEY",
    "openai_api_key": "YOUR_OPENAI_API_KEY",
    "elevenlabs_api_key": "YOUR_ELEVENLABS_API_KEY",
    "memory_db": "avatar_memory.db",
    "voice_model": "en-US-Wavenet-F",
    "auto_save": True,
    "god_mode": True,
    "browser_headless": False,
    "website_templates_dir": "./templates/",
    "data_storage": "./avatar_data/"
}

# Initialize APIs
genai.configure(api_key=CONFIG["gemini_api_key"])
openai.api_key = CONFIG["openai_api_key"]

# ============================================
# MEMORY ENGINE - PERMANENT STORAGE
# ============================================

@dataclass
class Memory:
    timestamp: str
    input_type: str  # text, voice, image, command
    input_data: str
    response: str
    emotion: str
    context: str
    tags: List[str]
    importance: int  # 1-10
    memory_id: str

class MemoryEngine:
    def __init__(self):
        self.conn = sqlite3.connect(CONFIG["memory_db"], check_same_thread=False)
        self.cursor = self.conn.cursor()
        self._create_tables()
        self.redis_client = redis.Redis(host='localhost', port=6379, decode_responses=True)
        
    def _create_tables(self):
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS memories (
                memory_id TEXT PRIMARY KEY,
                timestamp TEXT,
                input_type TEXT,
                input_data TEXT,
                response TEXT,
                emotion TEXT,
                context TEXT,
                tags TEXT,
                importance INTEGER
            )
        ''')
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS learned_patterns (
                pattern_id TEXT PRIMARY KEY,
                pattern TEXT,
                response TEXT,
                frequency INTEGER
            )
        ''')
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS website_templates (
                template_id TEXT PRIMARY KEY,
                name TEXT,
                html TEXT,
                css TEXT,
                js TEXT,
                created_at TEXT
            )
        ''')
        self.conn.commit()

    def save_memory(self, memory: Memory) -> str:
        memory.memory_id = hashlib.md5(f"{memory.timestamp}{memory.input_data}".encode()).hexdigest()
        self.cursor.execute('''
            INSERT OR REPLACE INTO memories 
            (memory_id, timestamp, input_type, input_data, response, emotion, context, tags, importance)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            memory.memory_id, memory.timestamp, memory.input_type,
            memory.input_data, memory.response, memory.emotion,
            memory.context, ','.join(memory.tags), memory.importance
        ))
        self.conn.commit()
        
        # Redis cache for fast access
        self.redis_client.setex(f"memory:{memory.memory_id}", 3600, json.dumps(asdict(memory)))
        return memory.memory_id

    def get_memory(self, memory_id: str) -> Optional[Memory]:
        cached = self.redis_client.get(f"memory:{memory_id}")
        if cached:
            data = json.loads(cached)
            return Memory(**data)
            
        self.cursor.execute('SELECT * FROM memories WHERE memory_id = ?', (memory_id,))
        row = self.cursor.fetchone()
        if row:
            return Memory(
                memory_id=row[0], timestamp=row[1], input_type=row[2],
                input_data=row[3], response=row[4], emotion=row[5],
                context=row[6], tags=row[7].split(',') if row[7] else [],
                importance=row[8]
            )
        return None

    def search_memories(self, query: str, limit: int = 10) -> List[Memory]:
        # Semantic search using Gemini
        prompt = f"Search my memory database for: {query}. Return relevant results."
        # Simplified: search by tags and input_data
        self.cursor.execute('''
            SELECT * FROM memories 
            WHERE input_data LIKE ? OR tags LIKE ? 
            ORDER BY importance DESC 
            LIMIT ?
        ''', (f'%{query}%', f'%{query}%', limit))
        rows = self.cursor.fetchall()
        return [Memory(
            memory_id=row[0], timestamp=row[1], input_type=row[2],
            input_data=row[3], response=row[4], emotion=row[5],
            context=row[6], tags=row[7].split(',') if row[7] else [],
            importance=row[8]
        ) for row in rows]

    def save_learned_pattern(self, pattern: str, response: str):
        pattern_id = hashlib.md5(pattern.encode()).hexdigest()
        self.cursor.execute('''
            INSERT OR REPLACE INTO learned_patterns (pattern_id, pattern, response, frequency)
            VALUES (?, ?, ?, COALESCE((SELECT frequency + 1 FROM learned_patterns WHERE pattern_id = ?), 1))
        ''', (pattern_id, pattern, response, pattern_id))
        self.conn.commit()

    def get_learned_response(self, input_text: str) -> Optional[str]:
        self.cursor.execute('''
            SELECT response FROM learned_patterns 
            WHERE pattern LIKE ? 
            ORDER BY frequency DESC 
            LIMIT 1
        ''', (f'%{input_text}%',))
        row = self.cursor.fetchone()
        return row[0] if row else None

    def close(self):
        self.conn.close()

# ============================================
# VOICE ENGINE - SPEAK & CLONE
# ============================================

class VoiceEngine:
    def __init__(self):
        self.recognizer = sr.Recognizer()
        self.whisper_model = whisper.load_model("base")
        self.tts_engine = pyttsx3.init()
        self.voices = self.tts_engine.getProperty('voices')
        self.current_voice = CONFIG["voice_model"]

    def listen(self, duration: int = 5) -> str:
        with sr.Microphone() as source:
            print("🎤 Listening...")
            self.recognizer.adjust_for_ambient_noise(source)
            try:
                audio = self.recognizer.listen(source, timeout=duration)
                text = self.recognizer.recognize_google(audio)
                print(f"🗣️ You said: {text}")
                return text
            except sr.UnknownValueError:
                return "Sorry, I couldn't understand."
            except sr.RequestError:
                return "Speech service unavailable."

    def speak(self, text: str, voice_clone: bool = False, clone_audio_path: str = None):
        if voice_clone and clone_audio_path:
            # Use ElevenLabs for voice cloning
            try:
                audio = generate(
                    text=text,
                    voice=VoiceSettings(
                        stability=0.5,
                        similarity_boost=0.75
                    ),
                    model="eleven_multilingual_v2"
                )
                play(audio)
                return
            except:
                pass  # Fallback to default TTS
        
        # Default TTS
        self.tts_engine.say(text)
        self.tts_engine.runAndWait()

    def transcribe_audio(self, audio_path: str) -> str:
        result = self.whisper_model.transcribe(audio_path)
        return result["text"]

    def clone_voice(self, audio_samples: List[str], voice_name: str) -> str:
        # Using ElevenLabs Voice Cloning
        try:
            voice = clone(
                name=voice_name,
                files=audio_samples,
                description=f"Cloned voice for {voice_name}"
            )
            return voice.voice_id
        except Exception as e:
            print(f"Voice cloning error: {e}")
            return None

# ============================================
# WEBSITE BUILDER ENGINE
# ============================================

class WebsiteBuilder:
    def __init__(self):
        self.templates_dir = CONFIG["website_templates_dir"]
        os.makedirs(self.templates_dir, exist_ok=True)
        self.memory = MemoryEngine()
        self.templates_cache = {}

    def create_template(self, name: str, html: str, css: str = "", js: str = "") -> str:
        template_id = hashlib.md5(f"{name}{datetime.datetime.now()}".encode()).hexdigest()
        
        # Save template to database
        self.memory.cursor.execute('''
            INSERT OR REPLACE INTO website_templates (template_id, name, html, css, js, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (template_id, name, html, css, js, datetime.datetime.now().isoformat()))
        self.memory.conn.commit()
        
        # Save as files
        template_dir = os.path.join(self.templates_dir, template_id)
        os.makedirs(template_dir, exist_ok=True)
        
        with open(os.path.join(template_dir, "index.html"), "w") as f:
            f.write(html)
        with open(os.path.join(template_dir, "style.css"), "w") as f:
            f.write(css)
        with open(os.path.join(template_dir, "script.js"), "w") as f:
            f.write(js)
            
        self.templates_cache[template_id] = {
            "name": name,
            "html": html,
            "css": css,
            "js": js
        }
        return template_id

    def generate_website_from_prompt(self, prompt: str) -> Dict[str, str]:
        # Use Gemini to generate website code
        gemini = genai.GenerativeModel('gemini-pro')
        response = gemini.generate_content(f"""
            Generate a complete modern website based on this description: {prompt}
            
            Return ONLY JSON in this format:
            {{
                "html": "FULL HTML CODE",
                "css": "FULL CSS CODE",
                "js": "FULL JAVASCRIPT CODE",
                "name": "Website Name"
            }}
            
            Make it responsive, beautiful, and functional.
            Include: header, hero section, features, footer.
            Use gradient colors, animations, and modern design.
        """)
        
        try:
            # Parse the response
            import json
            code = response.text
            # Extract JSON from the response
            json_match = re.search(r'\{.*\}', code, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
                return data
        except:
            pass
            
        # Fallback template
        return {
            "html": '<!DOCTYPE html><html><head><title>Your Site</title><link rel="stylesheet" href="style.css"></head><body><h1>Welcome</h1><p>Generated by AI</p><script src="script.js"></script></body></html>',
            "css": "body { font-family: Arial; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; text-align: center; padding: 50px; }",
            "js": "console.log('Hello from AI!');",
            "name": "AI Generated Site"
        }

    def get_template(self, template_id: str) -> Dict:
        if template_id in self.templates_cache:
            return self.templates_cache[template_id]
            
        self.memory.cursor.execute('SELECT name, html, css, js FROM website_templates WHERE template_id = ?', (template_id,))
        row = self.memory.cursor.fetchone()
        if row:
            return {"name": row[0], "html": row[1], "css": row[2], "js": row[3]}
        return None

    def deploy_website(self, template_id: str, domain: str = None) -> str:
        # Generate deployment URL
        template_dir = os.path.join(self.templates_dir, template_id)
        if not os.path.exists(template_dir):
            return "Template not found"
            
        # In production, this would deploy to a server
        # For now, return local path
        return f"file://{os.path.abspath(os.path.join(template_dir, 'index.html'))}"

# ============================================
# BROWSER & WEB AGENT
# ============================================

class BrowserAgent:
    def __init__(self):
        self.options = Options()
        if CONFIG["browser_headless"]:
            self.options.add_argument('--headless')
        self.driver = webdriver.Chrome(options=self.options)
        self.memory = MemoryEngine()

    def browse(self, url: str) -> str:
        try:
            self.driver.get(url)
            return self.driver.page_source
        except Exception as e:
            return f"Error: {e}"

    def search_web(self, query: str) -> List[Dict]:
        # Use Google search via selenium or requests
        try:
            self.driver.get(f"https://www.google.com/search?q={query}")
            results = []
            elements = self.driver.find_elements_by_css_selector('div.g')
            for el in elements[:5]:
                try:
                    title = el.find_element_by_css_selector('h3').text
                    link = el.find_element_by_css_selector('a').get_attribute('href')
                    snippet = el.find_element_by_css_selector('div.VwiC3b').text
                    results.append({"title": title, "link": link, "snippet": snippet})
                except:
                    continue
            return results
        except Exception as e:
            return [{"error": str(e)}]

    def extract_content(self, url: str) -> Dict:
        try:
            response = requests.get(url)
            soup = BeautifulSoup(response.text, 'html.parser')
            return {
                "title": soup.title.string if soup.title else "",
                "text": soup.get_text()[:5000],
                "links": [a.get('href') for a in soup.find_all('a') if a.get('href')],
                "images": [img.get('src') for img in soup.find_all('img') if img.get('src')]
            }
        except Exception as e:
            return {"error": str(e)}

    def close(self):
        self.driver.quit()

# ============================================
# GOD MODE - MASTER CONTROLLER
# ============================================

class GodModeAvatar:
    def __init__(self):
        self.name = CONFIG["avatar_name"]
        self.memory = MemoryEngine()
        self.voice = VoiceEngine()
        self.website = WebsiteBuilder()
        self.browser = BrowserAgent()
        self.gemini = genai.GenerativeModel('gemini-pro')
        self.context = []
        self.conversation_history = []
        self.auto_save = CONFIG["auto_save"]
        self.god_mode = CONFIG["god_mode"]
        
        print(f"🧠 {self.name} initialized in GOD MODE")
        print(f"💾 Auto-save: {'ON' if self.auto_save else 'OFF'}")
        print("="*50)

    def process_input(self, input_text: str, input_type: str = "text") -> str:
        # Step 1: Check learned patterns
        learned_response = self.memory.get_learned_response(input_text)
        if learned_response and self.god_mode:
            return learned_response

        # Step 2: Search memory for context
        relevant_memories = self.memory.search_memories(input_text, limit=3)
        memory_context = "\n".join([m.input_data + ": " + m.response for m in relevant_memories])

        # Step 3: Build prompt with full context
        prompt = f"""
        You are {self.name}, a super-intelligent AI avatar assistant.
        Your creator is {CONFIG['creator']}.
        
        Current conversation context:
        {self.context[-5:] if self.context else 'No recent context'}
        
        Relevant memories:
        {memory_context if memory_context else 'No relevant memories'}
        
        User input: {input_text}
        
        Respond naturally, helpfully, and intelligently.
        If the user asks you to do something (like build a website, search the web, speak, etc.), include the action in your response.
        
        Format: RESPONSE: [Your reply]
        ACTION: [Optional: website_builder|browser_search|speak|clone_voice|write_file|read_file]
        DATA: [Optional: details for the action]
        """

        # Step 4: Get response from Gemini
        response = self.gemini.generate_content(prompt)
        response_text = response.text

        # Step 5: Parse response
        reply = ""
        action = None
        data = None
        
        lines = response_text.split('\n')
        for line in lines:
            if line.startswith('RESPONSE:'):
                reply = line.replace('RESPONSE:', '').strip()
            elif line.startswith('ACTION:'):
                action = line.replace('ACTION:', '').strip()
            elif line.startswith('DATA:'):
                data = line.replace('DATA:', '').strip()

        # Step 6: Execute action if needed
        if action and data:
            reply += "\n\n" + self.execute_action(action, data)

        # Step 7: Save to memory
        memory = Memory(
            timestamp=datetime.datetime.now().isoformat(),
            input_type=input_type,
            input_data=input_text,
            response=reply,
            emotion="neutral",  # Could be detected
            context=str(self.context[-5:]),
            tags=self._extract_tags(input_text + " " + reply),
            importance=5
        )
        self.memory.save_memory(memory)
        
        # Step 8: Learn patterns
        if len(input_text) > 20:
            self.memory.save_learned_pattern(input_text, reply)

        # Step 9: Update context
        self.context.append({"input": input_text, "response": reply})
        if len(self.context) > 50:
            self.context = self.context[-50:]

        self.conversation_history.append({"user": input_text, "assistant": reply})

        return reply

    def execute_action(self, action: str, data: str) -> str:
        if action == "website_builder":
            return self._action_website_builder(data)
        elif action == "browser_search":
            return self._action_browser_search(data)
        elif action == "speak":
            return self._action_speak(data)
        elif action == "clone_voice":
            return self._action_clone_voice(data)
        elif action == "write_file":
            return self._action_write_file(data)
        elif action == "read_file":
            return self._action_read_file(data)
        else:
            return f"Action {action} not recognized."

    def _action_website_builder(self, data: str) -> str:
        try:
            # Parse data
            import json
            params = json.loads(data)
            prompt = params.get('prompt', data)
            
            # Generate website
            website_data = self.website.generate_website_from_prompt(prompt)
            template_id = self.website.create_template(
                name=website_data.get('name', 'AI Site'),
                html=website_data.get('html', ''),
                css=website_data.get('css', ''),
                js=website_data.get('js', '')
            )
            
            deploy_url = self.website.deploy_website(template_id)
            return f"✅ Website created! Template ID: {template_id}\nPreview: {deploy_url}"
        except Exception as e:
            return f"❌ Website build failed: {e}"

    def _action_browser_search(self, data: str) -> str:
        results = self.browser.search_web(data)
        if results:
            return "\n".join([f"• {r['title']}: {r['snippet']}" for r in results[:3]])
        return "No results found."

    def _action_speak(self, data: str) -> str:
        self.voice.speak(data)
        return f"🗣️ Spoke: {data}"

    def _action_clone_voice(self, data: str) -> str:
        # data should be path to audio files or voice name
        # For simplicity, we'll just simulate
        return f"🎙️ Voice cloning initiated for: {data}"

    def _action_write_file(self, data: str) -> str:
        try:
            import json
            params = json.loads(data)
            filename = params.get('filename', 'output.txt')
            content = params.get('content', '')
            path = os.path.join(CONFIG["data_storage"], filename)
            os.makedirs(CONFIG["data_storage"], exist_ok=True)
            with open(path, 'w') as f:
                f.write(content)
            return f"📄 File written: {path}"
        except Exception as e:
            return f"❌ Write failed: {e}"

    def _action_read_file(self, data: str) -> str:
        try:
            path = os.path.join(CONFIG["data_storage"], data)
            with open(path, 'r') as f:
                content = f.read()
            return f"📄 Content:\n{content[:1000]}"
        except Exception as e:
            return f"❌ Read failed: {e}"

    def _extract_tags(self, text: str) -> List[str]:
        # Simple tag extraction
        words = text.split()
        tags = []
        for word in words:
            if len(word) > 4 and word.isalnum():
                tags.append(word.lower())
        return list(set(tags[:5]))

    def listen_and_respond(self, duration: int = 5):
        voice_input = self.voice.listen(duration)
        if voice_input and voice_input != "Sorry, I couldn't understand.":
            response = self.process_input(voice_input, input_type="voice")
            self.voice.speak(response)
            return response
        return "Couldn't hear you."

    def export_all_data(self) -> Dict:
        # Export everything for backup
        data = {
            "memories": [],
            "learned_patterns": [],
            "templates": [],
            "context": self.context,
            "conversations": self.conversation_history
        }
        
        # Get memories
        self.memory.cursor.execute('SELECT * FROM memories')
        for row in self.memory.cursor.fetchall():
            data["memories"].append({
                "memory_id": row[0],
                "timestamp": row[1],
                "input_type": row[2],
                "input_data": row[3],
                "response": row[4],
                "emotion": row[5],
                "context": row[6],
                "tags": row[7],
                "importance": row[8]
            })
        
        # Get learned patterns
        self.memory.cursor.execute('SELECT * FROM learned_patterns')
        for row in self.memory.cursor.fetchall():
            data["learned_patterns"].append({
                "pattern_id": row[0],
                "pattern": row[1],
                "response": row[2],
                "frequency": row[3]
            })
        
        # Get templates
        self.memory.cursor.execute('SELECT * FROM website_templates')
        for row in self.memory.cursor.fetchall():
            data["templates"].append({
                "template_id": row[0],
                "name": row[1],
                "html": row[2],
                "css": row[3],
                "js": row[4],
                "created_at": row[5]
            })
        
        return data

    def import_data(self, data: Dict) -> str:
        # Import all data
        try:
            # Import memories
            for mem in data.get('memories', []):
                memory = Memory(
                    timestamp=mem['timestamp'],
                    input_type=mem['input_type'],
                    input_data=mem['input_data'],
                    response=mem['response'],
                    emotion=mem['emotion'],
                    context=mem['context'],
                    tags=mem['tags'].split(',') if isinstance(mem['tags'], str) else [],
                    importance=mem['importance']
                )
                self.memory.save_memory(memory)
            
            # Import learned patterns
            for pat in data.get('learned_patterns', []):
                self.memory.save_learned_pattern(pat['pattern'], pat['response'])
            
            # Import templates
            for temp in data.get('templates', []):
                self.website.create_template(
                    name=temp['name'],
                    html=temp['html'],
                    css=temp['css'],
                    js=temp['js']
                )
            
            return f"✅ Imported {len(data.get('memories', []))} memories, {len(data.get('learned_patterns', []))} patterns, {len(data.get('templates', []))} templates"
        except Exception as e:
            return f"❌ Import failed: {e}"

    def generate_prompt(self, task: str) -> str:
        # Generate powerful prompts for any task
        prompt = f"""
        Generate a highly detailed, optimized prompt for the following task: {task}
        
        The prompt should be:
        1. Clear and specific
        2. Include all necessary context
        3. Define the expected output format
        4. Include constraints and requirements
        5. Be structured for maximum AI performance
        
        Output only the prompt, no explanation.
        """
        response = self.gemini.generate_content(prompt)
        return response.text

    def auto_save_all(self):
        if self.auto_save:
            data = self.export_all_data()
            save_path = os.path.join(CONFIG["data_storage"], f"backup_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
            os.makedirs(CONFIG["data_storage"], exist_ok=True)
            with open(save_path, 'w') as f:
                json.dump(data, f, indent=2)
            return save_path
        return None

    def close(self):
        self.browser.close()
        self.memory.close()
        self.auto_save_all()
        print("🛑 Avatar shutdown complete.")

# ============================================
# GRADIO UI - WEB INTERFACE
# ============================================

def create_ui(avatar: GodModeAvatar):
    def chat_with_avatar(message, history):
        response = avatar.process_input(message, "text")
        return response

    def voice_chat():
        return avatar.listen_and_respond()

    def build_site(prompt):
        return avatar.website.generate_website_from_prompt(prompt)

    def search_web(query):
        return avatar.browser.search_web(query)

    def save_backup():
        path = avatar.auto_save_all()
        return f"✅ Backup saved: {path}" if path else "⚠️ Auto-save disabled"

    def export_data():
        return json.dumps(avatar.export_all_data(), indent=2)

    def import_data_file(file):
        data = json.load(file.name)
        return avatar.import_data(data)

    with gr.Blocks(title=f"{avatar.name} - God Mode AI Avatar", theme=gr.themes.Soft()) as demo:
        gr.Markdown(f"# 🧠 {avatar.name} - God Mode AI Avatar")
        gr.Markdown(f"### Creator: {CONFIG['creator']}")
        
        with gr.Tab("💬 Chat"):
            chatbot = gr.Chatbot()
            msg = gr.Textbox(label="Your Message")
            clear = gr.Button("Clear")
            
            msg.submit(chat_with_avatar, [msg, chatbot], [chatbot, msg])
            clear.click(lambda: None, None, chatbot, queue=False)

        with gr.Tab("🎤 Voice"):
            voice_btn = gr.Button("🎙️ Listen & Respond")
            voice_output = gr.Textbox(label="Voice Response")
            voice_btn.click(voice_chat, None, voice_output)

        with gr.Tab("🌐 Website Builder"):
            site_prompt = gr.Textbox(label="Describe your website", placeholder="A modern portfolio for a photographer...")
            site_btn = gr.Button("🚀 Generate Website")
            site_output = gr.HTML(label="Generated Website Preview")
            site_btn.click(build_site, site_prompt, site_output)

        with gr.Tab("🔍 Browser Search"):
            search_query = gr.Textbox(label="Search query")
            search_btn = gr.Button("Search")
            search_results = gr.JSON(label="Results")
            search_btn.click(search_web, search_query, search_results)

        with gr.Tab("💾 Data Management"):
            export_btn = gr.Button("📤 Export All Data")
            export_output = gr.JSON(label="Exported Data")
            export_btn.click(export_data, None, export_output)
            
            import_file = gr.File(label="Import JSON Backup")
            import_btn = gr.Button("📥 Import Data")
            import_output = gr.Textbox(label="Import Status")
            import_btn.click(import_data_file, import_file, import_output)
            
            backup_btn = gr.Button("💾 Auto Save Now")
            backup_output = gr.Textbox(label="Backup Status")
            backup_btn.click(save_backup, None, backup_output)

        with gr.Tab("⚡ Power Prompts"):
            task_input = gr.Textbox(label="What prompt do you need?", placeholder="Write a prompt to generate a marketing strategy...")
            generate_btn = gr.Button("🔮 Generate Prompt")
            prompt_output = gr.Textbox(label="Generated Prompt", lines=10)
            generate_btn.click(avatar.generate_prompt, task_input, prompt_output)

        with gr.Tab("🧠 Memory"):
            memory_query = gr.Textbox(label="Search memories")
            memory_btn = gr.Button("Search")
            memory_output = gr.JSON(label="Relevant Memories")
            
            def search_memories(query):
                memories = avatar.memory.search_memories(query)
                return [{"input": m.input_data, "response": m.response, "importance": m.importance} for m in memories]
            
            memory_btn.click(search_memories, memory_query, memory_output)

    return demo

# ============================================
# MAIN - LAUNCH EVERYTHING
# ============================================

def main():
    print("🚀 Initializing God Mode Avatar...")
    avatar = GodModeAvatar()
    
    # Run auto-save thread
    def auto_save_loop():
        import time
        while True:
            time.sleep(300)  # Every 5 minutes
            avatar.auto_save_all()
    
    save_thread = threading.Thread(target=auto_save_loop, daemon=True)
    save_thread.start()
    
    # Launch Gradio UI
    ui = create_ui(avatar)
    ui.launch(share=True, server_name="0.0.0.0", server_port=7860)
    
    # Cleanup
    avatar.close()

if __name__ == "__main__":
    main()