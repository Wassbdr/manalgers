import React from "react";
import SpeechRecognition, { useSpeechRecognition } from "react-speech-recognition";

export default function SpeechToText() {
  const { transcript, listening, resetTranscript } = useSpeechRecognition();

  if (!SpeechRecognition.browserSupportsSpeechRecognition()) {
    return <p>Your browser does not support speech recognition.</p>;
  }

  return (
    <div style={{ padding: "20px", textAlign: "center" }}>
      <h2>Speech to Text Demo</h2>

      <button
        onClick={() => SpeechRecognition.startListening({ continuous: true })}
        style={{ margin: "5px", padding: "10px 20px" }}
      >
        🎤 Start
      </button>

      <button
        onClick={SpeechRecognition.stopListening}
        style={{ margin: "5px", padding: "10px 20px" }}
      >
        ⏹ Stop
      </button>

      <button
        onClick={resetTranscript}
        style={{ margin: "5px", padding: "10px 20px" }}
      >
        ✖ Reset
      </button>

      <div style={{ marginTop: "20px", border: "1px solid #ccc", padding: "10px" }}>
        <p>{listening ? "🎧 Listening..." : "🛑 Not listening"}</p>
        <p><strong>Transcript:</strong> {transcript}</p>
      </div>
    </div>
  );
}