import React, { useRef, useState } from 'react';
import ReactDOM from 'react-dom/client';
import SpeechRecognition, { useSpeechRecognition } from 'react-speech-recognition';

function App() {
  const { transcript, listening, resetTranscript } = useSpeechRecognition();

  // Check if browser supports SpeechRecognition
  if (!SpeechRecognition.browserSupportsSpeechRecognition()) {
    return <p>Your browser does not support Speech Recognition.</p>;
  }

  return (
    <div style={{ padding: '40px', textAlign: 'center' }}>
      <h1>Welcome to AI Assistant ManAlgers</h1>

      <div style={{ marginTop: '20px' }}>
        <button
          onClick={() => SpeechRecognition.startListening({ continuous: true })}
          style={{ margin: '5px', padding: '10px 20px' }}
        >
          🎤 Start
        </button>

        <button
          onClick={SpeechRecognition.stopListening}
          style={{ margin: '5px', padding: '10px 20px' }}
        >
          ⏹ Stop
        </button>

        <button
          onClick={resetTranscript}
          style={{ margin: '5px', padding: '10px 20px' }}
        >
          ✖ Reset
        </button>
      </div>

      <div
        style={{
          marginTop: '20px',
          border: '1px solid #ccc',
          padding: '10px',
          minHeight: '100px'
        }}
      >
        <p>{listening ? '🎧 Listening...' : '🛑 Not listening'}</p>
        <p><strong>Transcript:</strong> {transcript}</p>
      </div>
    </div>
  );
}

const root = ReactDOM.createRoot(document.getElementById('root'));
root.render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);