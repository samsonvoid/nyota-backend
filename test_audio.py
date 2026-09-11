import os
import sys
import time
import pyttsx3

def speak(text):
    print(f"[Nyota]: {text}")
    try:
        engine = pyttsx3.init()
        voices = engine.getProperty('voices')
        if voices:
            # Set to David (usually index 0 on Windows)
            engine.setProperty('voice', voices[0].id)
        engine.setProperty('rate', 155) # Warm, professional pace
        engine.say(text)
        engine.runAndWait()
    except Exception as e:
        print(f"Speech error: {e}")

def test_mic():
    try:
        import pyaudio
        p = pyaudio.PyAudio()
        print("[System]: Initializing PyAudio...")
        
        # Open default input stream (16kHz mono is standard for speech-to-text)
        stream = p.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=16000,
            input=True,
            frames_per_buffer=1024
        )
        print("[System]: Microphone stream opened successfully.")
        
        speak("I am now listening. Please say something into the microphone.")
        print("[System]: Recording for 3 seconds...")
        
        frames = []
        for _ in range(0, int(16000 / 1024 * 3)):
            data = stream.read(1024)
            frames.append(data)
            
        print("[System]: Recording complete.")
        stream.stop_stream()
        stream.close()
        p.terminate()
        
        speak("Audio captured successfully. Microphone recording test passed!")
        
    except ImportError:
        print("[Error]: pyaudio is not installed globally or in Python 3.13.")
        speak("Py-audio library is not detected. Please install it using py -3.13 -m pip install pyaudio.")
    except Exception as e:
        print(f"[Error]: Microphone test failed: {e}")
        speak("Microphone test encountered an error.")

if __name__ == "__main__":
    speak("Hello Samson. Welcome to Nyota Assistant audio diagnostics. Starting startup audio check.")
    test_mic()
