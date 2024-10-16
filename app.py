import os
from flask import Flask, request, jsonify
import youtube_transcript_api
import requests
import json
import re
import firebase_admin
from firebase_admin import credentials, db

app = Flask(__name__)

# Initialize Firebase
cred = credentials.Certificate(json.loads(os.environ.get('FIREBASE_ADMIN_CONFIG')))
firebase_admin.initialize_app(cred, {
    'databaseURL': os.environ.get('FIREBASE_DATABASE_URL')
})

GEMINI_API_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash-latest:generateContent"
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')

def get_youtube_transcript(video_url):
    video_id_match = re.search(r'(?:v=|\/)([0-9A-Za-z_-]{11}).*', video_url)
    if video_id_match:
        video_id = video_id_match.group(1)
    else:
        print("Error: Could not extract video ID from the provided URL.")
        return None
    try:
        transcript = youtube_transcript_api.YouTubeTranscriptApi.get_transcript(video_id)
        return " ".join([entry['text'] for entry in transcript])
    except Exception as e:
        print(f"Error getting transcript: {e}")
        return None

def call_gemini_api(prompt):
    headers = {
        'Content-Type': 'application/json'
    }
    data = {
        "contents": [{"parts": [{"text": prompt}]}]
    }
    response = requests.post(f"{GEMINI_API_ENDPOINT}?key={GEMINI_API_KEY}", 
                             headers=headers, 
                             data=json.dumps(data))
    if response.status_code == 200:
        try:
            return response.json()['candidates'][0]['content']['parts'][0]['text']
        except KeyError as e:
            print(f"Unexpected response structure. KeyError: {e}")
            print("Response JSON:")
            print(json.dumps(response.json(), indent=2))
            return None
    else:
        print(f"Error calling Gemini API: {response.status_code}")
        print(response.text)
        return None

def segment_transcript(transcript):
    prompt = f"""Divide the following transcript into logical passages. Each passage should have a clear beginning and end, covering a complete thought or topic. Provide a title for each passage. Format the output as follows:

    //Title 1
    Passage 1 content...

    //Title 2
    Passage 2 content...

    (and so on)

    //END

    Here's the transcript:

    {transcript}
    """
    return call_gemini_api(prompt)

def convert_segment_to_markdown(segment, title):
    prompt = f"""Create comprehensive notes in Markdown format for the following text. 
    The notes should:
    1. Use "{title}" as the main heading
    2. Have a clear structure with subheadings
    3. Include key points and important details
    4. Use bullet points or numbered lists where appropriate
    5. Be concise yet informative

    Here's the text to create notes for:

    {segment}

    Please provide only the Markdown formatted notes, without any additional explanation.
    """
    return call_gemini_api(prompt)

@app.route('/process', methods=['POST'])
def process():
    request_id = request.json['request_id']
    video_url = request.json['video_url']
    
    # Update request status in Firebase
    db.reference(f'mindmap-requests/{request_id}').update({'status': 'processing'})

    transcript = get_youtube_transcript(video_url)
    if not transcript:
        db.reference(f'mindmap-requests/{request_id}').update({'status': 'failed', 'error': 'Failed to get transcript'})
        return jsonify({"error": "Failed to get transcript"}), 400

    segmented_transcript = segment_transcript(transcript)
    if not segmented_transcript:
        db.reference(f'mindmap-requests/{request_id}').update({'status': 'failed', 'error': 'Failed to segment transcript'})
        return jsonify({"error": "Failed to segment transcript"}), 400

    segments = re.split(r'(?=//)', segmented_transcript)[1:-1]

    markdown_segments = []
    for i, segment in enumerate(segments, 1):
        title, content = segment.split("\n", 1)
        title = title.strip("//").strip()
        content = content.strip()
        markdown = convert_segment_to_markdown(content, title)
        if markdown is None:
            continue
        markdown_segments.append(markdown)

    if not markdown_segments:
        db.reference(f'mindmap-requests/{request_id}').update({'status': 'failed', 'error': 'No segments were successfully processed'})
        return jsonify({"error": "No segments were successfully processed"}), 400

    final_markdown = "\n\n---\n\n".join(markdown_segments)

    # Update Firebase with the final markdown
    db.reference(f'mindmap-requests/{request_id}').update({
        'status': 'completed',
        'markdown': final_markdown
    })

    return jsonify({"status": "success", "message": "Mindmap generated successfully"})

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
