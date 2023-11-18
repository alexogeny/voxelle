#!/usr/bin/env python3

import asyncio
import base64
import configparser
import json
import logging
import shutil
import subprocess
import uuid

import click
import httpx
import websockets

config = configparser.ConfigParser()
logger = logging.getLogger(__name__)


def test_api_key(api_key):
    """Test the API key."""
    try:
        response = httpx.get(
            "https://api.elevenlabs.io/v1/models", headers={"xi-api-key": api_key}
        )
        response.raise_for_status()
        return True
    except httpx.HTTPStatusError:
        return False


def prompt_user_choice(options):
    """Prompt user to choose an option by number."""
    for index, option in enumerate(options, start=1):
        click.echo(f"{index}: {option['name']}")

    choice = click.prompt("Please choose a number", type=int, default=1)
    return options[choice - 1].get("id") if 0 < choice <= len(options) else None


def fetch_data(api_key, url):
    """Fetch data from the given URL using the API key."""
    response = httpx.get(url, headers={"xi-api-key": api_key})
    return response.json()


@click.command()
def interactive_cli():
    """Interactive CLI for setting up and using the ElevenLabs API."""
    click.echo("Welcome to the ElevenLabs CLI Setup")

    # API Key Input
    config.read("config.ini")
    api_key = config.get("Preferences", "xi-api-key", fallback=None)
    if api_key is None or not test_api_key(api_key):
        while True:
            api_key = click.prompt("Please enter your ElevenLabs API key")
            if test_api_key(api_key):
                config["Preferences"] = {"xi-api-key": api_key}
                save_config()
                click.echo("API key saved.")
                break
            else:
                click.echo("Invalid API key. Please try again.")

    # Model Selection
    # check if models exist in config
    if "Models" in config:
        available_models = config["Models"]
    else:
        models = fetch_data(api_key, "https://api.elevenlabs.io/v1/models")
        available_models = {model["model_id"]: model["name"] for model in models}
        config["Models"] = available_models
        save_config()

    # if model_id is not set in the config, ask user to choose a model
    if "model_id" not in config["Preferences"]:
        click.echo("Available Models:")
        model_choice = prompt_user_choice(
            [
                {"name": name, "id": model_id}
                for model_id, name in available_models.items()
            ]
        )
        click.echo(f"Selected model: {model_choice}")
        # save model choice to config
        config["Preferences"]["model_id"] = model_choice
        save_config()
    else:
        model_choice = config["Preferences"]["model_id"]

    # Voice Selection
    if "Voices" in config:
        cloned_voices = config["Voices"]
    else:
        voices = fetch_data(api_key, "https://api.elevenlabs.io/v1/voices")
        cloned_voices = [
            voice for voice in voices["voices"] if voice["category"] == "cloned"
        ]
        config["Voices"] = {voice["voice_id"]: voice["name"] for voice in cloned_voices}
        save_config()

    if "voice_id" not in config["Preferences"]:
        click.echo("Available Voices:")
        selected_voice = prompt_user_choice(
            [{"name": name, "id": voice_id} for voice_id, name in cloned_voices.items()]
        )

        while selected_voice is None:
            click.echo("Invalid choice.")
            selected_voice = prompt_user_choice(
                [
                    {"name": name, "id": voice_id}
                    for voice_id, name in cloned_voices.items()
                ]
            )

        click.echo(f"You have selected the voice: {selected_voice}")
        # save voice choice to config
        config["Preferences"]["voice_id"] = selected_voice
        save_config()
    else:
        selected_voice = config["Preferences"]["voice_id"]

    # Text Input
    text_input = click.prompt(
        "Please enter the text you want to convert to speech", type=str
    )

    # async iterable of text (chunks)
    async def text_chunks(text):
        yield text

    # Text to Speech
    asyncio.run(stream_text_to_speech(text_chunks(text_input)))


def is_installed(lib_name):
    return shutil.which(lib_name) is not None


async def stream(audio_stream):
    """Stream audio data using mpv player."""

    filename = f"{uuid.uuid4()}.mp3"

    with open(filename, "wb") as f:
        mpv_process = subprocess.Popen(
            ["mpv", "--no-cache", "--no-terminal", "--", "fd://0"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        async for chunk in audio_stream:
            if chunk:
                f.write(chunk)
                mpv_process.stdin.write(chunk)
                mpv_process.stdin.flush()

        if mpv_process.stdin:
            mpv_process.stdin.close()
        mpv_process.wait()

    click.echo(f"Saved audio to {filename}")


async def text_chunker(chunks):
    """Split text into chunks, ensuring to not break sentences."""
    splitters = (".", ",", "?", "!", ";", ":", "—", "-", "(", ")", "[", "]", "}", " ")
    buffer = ""

    async for text in chunks:
        if buffer.endswith(splitters):
            yield buffer + " "
            buffer = text
        elif text.startswith(splitters):
            yield buffer + text[0] + " "
            buffer = text[1:]
        else:
            buffer += text

    if buffer:
        yield buffer + " "


async def stream_text_to_speech(text):
    XI_API_KEY = config["Preferences"]["xi-api-key"]
    MODEL_ID = config["Preferences"]["model_id"]
    VOICE_ID = config["Preferences"]["voice_id"]
    uri = f"wss://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}/stream-input?model_id={MODEL_ID}"
    async with websockets.connect(uri) as websocket:
        # Send text
        message = {
            "text": " ",
            "xi_api_key": XI_API_KEY,
            "voice_settings": {"stability": 0.7},
        }
        await websocket.send(json.dumps(message))

        async def listen():
            while True:
                try:
                    message = await websocket.recv()
                    data = json.loads(message)
                    if data.get("audio"):
                        yield base64.b64decode(data["audio"])
                    elif data.get("isFinal"):
                        break
                except websockets.exceptions.ConnectionClosed:
                    break

        listen_task = asyncio.create_task(stream(listen()))

        async for text_entry in text_chunker(text):
            await websocket.send(
                json.dumps({"text": text_entry, "try_trigger_generation": True})
            )
        await websocket.send(json.dumps({"text": ""}))
        await listen_task


def save_config():
    with open("config.ini", "w") as configfile:
        config.write(configfile)

    # Process Text (You can add your text-to-speech processing logic here)


if __name__ == "__main__":
    if not is_installed("mpv"):
        print("Please install mpv first")
        exit(1)
    interactive_cli()
