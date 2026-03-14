import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ActionBar from "./components/ActionBar";
import AlertCenter from "./components/AlertCenter";
import ConnectionIndicator from "./components/ConnectionIndicator";
import EventAlertStrip from "./components/EventAlertStrip";
import MemoriesPanel from "./components/MemoriesPanel";
import TranscriptPanel from "./components/TranscriptPanel";
import UserTab from "./components/UserTab";

const WEBHOOK_TOKEN = import.meta.env.VITE_WEBHOOK_TOKEN ?? "demo-webhook-token";

async function fetchJsonWithTimeout(url, options = {}, timeoutMs = 8000) {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(url, { ...options, signal: controller.signal });
    if (!res.ok) throw new Error(`non-ok status ${res.status}`);
    return await res.json();
  } finally {
    clearTimeout(timeoutId);
  }
}

function cleanMemoryText(text) {
  if (typeof text !== "string") return "";
  return text
    .replace(/^\[?\s*Memory Saved:\s*/i, "")
    .replace(/^\[|\]$/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

function getReminderAlertMessage(systemText) {
  if (typeof systemText !== "string") return null;
  const text = systemText.trim();
  const lower = text.toLowerCase();

  if (lower.includes("proactive whisper injected") || lower.includes("meeting starting")) {
    const compact = text.replace(/^\[|\]$/g, "");
    const match = compact.match(/Meeting starting with\s+(.+?)\.\s+Remember:\s+(.+)/i);
    if (match) {
      const attendee = match[1]?.trim();
      const reminder = match[2]?.trim();
      if (attendee && reminder) {
        return `Meeting with ${attendee}. Reminder: ${reminder}`;
      }
    }
    return "You have a meeting reminder now.";
  }

  if (lower.includes("calendar checked")) {
    return "You have upcoming items in your calendar.";
  }

  return null;
}

function isSystemNoiseText(text) {
  if (typeof text !== "string") return true;
  const lower = text.toLowerCase();
  return (
    lower.startsWith("proactive whisper injected") ||
    lower.startsWith("vision context saved") ||
    lower.startsWith("vapi inject") ||
    lower.startsWith("calendar checked")
  );
}

function isReminderLike(text) {
  if (typeof text !== "string") return false;
  const lower = text.toLowerCase();

  if (lower.length < 18) return false;
  return (
    lower.includes("meeting") ||
    lower.includes("remind") ||
    lower.includes("tomorrow") ||
    lower.includes("tonight") ||
    lower.includes("at ") ||
    lower.includes("before ")
  );
}

function isPrimaryUserMemory(item) {
  const category = String(item?.category ?? "").toLowerCase();
  return category === "voice";
}

function isProactiveTranscriptMessage(message) {
  if (!message) return false;
  const role = String(message.role ?? "").toLowerCase();
  const text = String(message.text ?? "").trim();
  if (role !== "assistant" || !text) return false;

  const lower = text.toLowerCase();
  return (
    lower.includes("aether proactive action") ||
    lower.includes("system alert:") ||
    lower.includes("proactive whisper injected") ||
    lower.includes("aether internal directive")
  );
}

function getRemainingTimerText(remainingMs) {
  const totalSeconds = Math.max(0, Math.ceil(remainingMs / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

function unitToSeconds(value, unitRaw) {
  const unit = String(unitRaw ?? "").toLowerCase();
  if (unit.startsWith("hour") || unit.startsWith("hr")) return Math.round(value * 3600);
  if (unit.startsWith("minute") || unit.startsWith("min")) return Math.round(value * 60);
  return Math.round(value);
}

function extractTimerInstruction(text) {
  if (typeof text !== "string") return null;
  const normalized = text.replace(/\s+/g, " ").trim();
  if (!normalized) return null;

  // Activate hidden timer only for explicit reminder intent.
  if (!/\bremind\b/i.test(normalized)) return null;

  const durationRegex = /\b(?:in|after)\s+(\d+(?:\.\d+)?)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?)\b/i;
  const match = normalized.match(durationRegex);
  if (!match) return null;

  const amount = Number(match[1]);
  if (!Number.isFinite(amount) || amount <= 0) return null;

  const durationSeconds = unitToSeconds(amount, match[2]);
  if (durationSeconds <= 0) return null;

  let label = normalized;
  label = label.replace(/^.*?\bremind(?:\s+me|\s+us)?\b/i, "").trim();
  label = label.replace(durationRegex, "").trim();
  label = label.replace(/^(to|about)\b\s*/i, "").trim();
  label = label.replace(/[.!?]+$/g, "").trim();

  if (!label) {
    label = "Your requested reminder";
  }

  return {
    durationSeconds,
    label,
  };
}

export default function App() {
  const [transcript, setTranscript] = useState([]);
  const [memories, setMemories] = useState([]);
  const [connected, setConnected] = useState(true);
  const [actionStatus, setActionStatus] = useState(null); // { loading, ok, message }
  const [alertEvent, setAlertEvent] = useState(null);
  const [alertEvents, setAlertEvents] = useState([]);
  const [activeTab, setActiveTab] = useState("user");
  const [listening, setListening] = useState(false);
  const [speechSupported, setSpeechSupported] = useState(false);
  const [demoBusy, setDemoBusy] = useState(false);
  const [timers, setTimers] = useState([]);
  const [nowMs, setNowMs] = useState(Date.now());

  const recognitionRef = useRef(null);
  const transcriptCountRef = useRef(0);
  const alertFingerprintRef = useRef(new Set());
  const proactiveFingerprintRef = useRef(new Set());
  const alertsInitializedRef = useRef(false);
  const resultReceivedRef = useRef(false);
  const speechVoiceRef = useRef(null);

  const refreshTranscript = useCallback(async () => {
    const data = await fetchJsonWithTimeout("/api/v1/transcript", {}, 8000);
    setTranscript(data.messages ?? []);
  }, []);

  const refreshMemories = useCallback(async () => {
    const data = await fetchJsonWithTimeout("/api/v1/memories", {}, 8000);
    setMemories(data.data ?? []);
  }, []);

  // ── Transcript polling — every 1500 ms ──────────────────────────────────
  useEffect(() => {
    let active = true;

    let timeoutId;

    const poll = async () => {
      if (!active) return;
      try {
        await refreshTranscript();
        if (active) {
          setConnected(true);
        }
      } catch {
        if (active) setConnected(false);
      } finally {
        if (active) {
          timeoutId = setTimeout(poll, 1500);
        }
      }
    };

    poll();
    return () => {
      active = false;
      clearTimeout(timeoutId);
    };
  }, [refreshTranscript]);

  // ── Memories polling — every 3000 ms ────────────────────────────────────
  useEffect(() => {
    let active = true;

    let timeoutId;

    const poll = async () => {
      if (!active) return;
      try {
        if (active) await refreshMemories();
      } catch {
        // Transcript polling already manages the connection indicator;
        // silently skip here to avoid duplicate error states.
      } finally {
        if (active) {
          timeoutId = setTimeout(poll, 3000);
        }
      }
    };

    poll();
    return () => {
      active = false;
      clearTimeout(timeoutId);
    };
  }, [refreshMemories]);

  const saveReminder = useCallback(
    async (factToRemember, category = "personal") => {
      setActionStatus({ loading: true });
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 10_000);
      try {
        const payload = {
          message: {
            type: "tool-calls",
            toolWithToolCallList: [
              {
                tool: { name: "save_user_memory" },
                toolCall: {
                  id: `ui-${Date.now()}`,
                  arguments: {
                    fact_to_remember: factToRemember,
                    category,
                  },
                },
              },
            ],
          },
        };

        const res = await fetch("/api/v1/webhook/save_memory", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Webhook-Token": WEBHOOK_TOKEN,
          },
          body: JSON.stringify(payload),
          signal: controller.signal,
        });

        const data = await res.json().catch(() => ({}));
        const isOk = res.ok && Array.isArray(data.results) && data.results.length > 0;

        setActionStatus({
          ok: isOk,
          message: isOk
            ? "Reminder stored in working memory"
            : `Could not store reminder (${res.status})`,
        });

        if (isOk) {
          await Promise.all([refreshTranscript(), refreshMemories()]);
        }
      } catch (err) {
        const isAbort = err?.name === "AbortError";
        setActionStatus({
          ok: false,
          message: isAbort ? "Request timed out" : "Connection failed",
        });
      } finally {
        clearTimeout(timeoutId);
      }
      setTimeout(() => setActionStatus(null), 3500);
    },
    [refreshMemories, refreshTranscript]
  );

  const speakAlert = useCallback((text) => {
    if (typeof window === "undefined" || !("speechSynthesis" in window)) return;
    const clean = String(text ?? "").trim();
    if (!clean) return;

    const synth = window.speechSynthesis;

    if (!speechVoiceRef.current) {
      const voices = synth.getVoices();
      const selected =
        voices.find((voice) => /^en(-|_)/i.test(voice.lang || "") && /female|samantha|aria|zira|ava/i.test(voice.name || "")) ||
        voices.find((voice) => /^en(-|_)/i.test(voice.lang || "")) ||
        voices[0] ||
        null;
      speechVoiceRef.current = selected;
    }

    const utterance = new SpeechSynthesisUtterance(clean);
    if (speechVoiceRef.current) {
      utterance.voice = speechVoiceRef.current;
      utterance.lang = speechVoiceRef.current.lang || "en-US";
    } else {
      utterance.lang = "en-US";
    }
    utterance.rate = 1;
    utterance.pitch = 1;
    utterance.volume = 1;

    synth.cancel();
    synth.speak(utterance);
  }, []);

  const pushAlert = useCallback((text) => {
    const event = {
      id: Date.now(),
      message: text,
      timestamp: new Date().toISOString(),
    };

    setAlertEvent(event);
    setAlertEvents((prev) => [event, ...prev].slice(0, 50));

    if (typeof window !== "undefined" && "Notification" in window) {
      if (window.Notification.permission === "granted") {
        new window.Notification("Aether Alert", { body: text });
      }
    }

    speakAlert(text);
  }, [speakAlert]);

  const playProactiveChime = useCallback(() => {
    if (typeof window === "undefined") return;

    const chimeUrl = "https://actions.google.com/sounds/v1/alarms/beep_short.ogg";
    try {
      const chime = new Audio(chimeUrl);
      chime.volume = 0.35;
      const playback = chime.play();
      if (playback && typeof playback.catch === "function") {
        playback.catch(() => {
          // ignored
        });
      }
      return;
    } catch {
      // ignored
    }

    const AudioContextType = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextType) return;

    try {
      const audioContext = new AudioContextType();
      const now = audioContext.currentTime;
      const gain = audioContext.createGain();
      gain.gain.setValueAtTime(0.0001, now);
      gain.gain.exponentialRampToValueAtTime(0.07, now + 0.01);
      gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.2);
      gain.connect(audioContext.destination);

      const osc = audioContext.createOscillator();
      osc.type = "sine";
      osc.frequency.setValueAtTime(1046, now);
      osc.connect(gain);
      osc.start(now);
      osc.stop(now + 0.2);

      setTimeout(() => {
        audioContext.close().catch(() => {
          // ignored
        });
      }, 300);
    } catch {
      // ignored
    }
  }, []);

  const startTimer = useCallback((durationSeconds, label, options = {}) => {
    const silent = Boolean(options?.silent);
    const parsed = Number(durationSeconds);
    if (!Number.isFinite(parsed) || parsed <= 0) {
      if (!silent) {
        setActionStatus({ ok: false, message: "Timer duration must be greater than 0 seconds" });
        setTimeout(() => setActionStatus(null), 3500);
      }
      return;
    }

    const boundedSeconds = Math.min(Math.floor(parsed), 24 * 60 * 60);
    const message = String(label ?? "").trim() || "Time is up";
    const id = `timer-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const endsAt = Date.now() + boundedSeconds * 1000;

    setTimers((prev) => [
      {
        id,
        label: message,
        durationSeconds: boundedSeconds,
        endsAt,
      },
      ...prev,
    ]);

    if (!silent) {
      setActionStatus({
        ok: true,
        message: `Reminder armed (${getRemainingTimerText(boundedSeconds * 1000)})`,
      });
      setTimeout(() => setActionStatus(null), 3500);
    }
  }, []);

  const cancelTimer = useCallback((timerId) => {
    setTimers((prev) => prev.filter((timer) => timer.id !== timerId));
  }, []);

  useEffect(() => {
    if (timers.length === 0) return;

    const intervalId = setInterval(() => {
      setNowMs(Date.now());
    }, 1000);

    return () => clearInterval(intervalId);
  }, [timers.length]);

  useEffect(() => {
    if (timers.length === 0) return;

    const expired = timers.filter((timer) => timer.endsAt <= nowMs);
    if (expired.length === 0) return;

    expired.forEach((timer) => {
      pushAlert(`Timer finished: ${timer.label}`);
    });

    setTimers((prev) => prev.filter((timer) => timer.endsAt > nowMs));
  }, [nowMs, pushAlert, timers]);

  useEffect(() => {
    if (!alertsInitializedRef.current) {
      transcriptCountRef.current = transcript.length;
      alertsInitializedRef.current = true;
      return;
    }

    if (transcript.length < transcriptCountRef.current) {
      transcriptCountRef.current = transcript.length;
      return;
    }

    const startIdx = transcriptCountRef.current;
    const endIdx = transcript.length;
    transcriptCountRef.current = endIdx;

    if (endIdx <= startIdx) return;

    const newMessages = transcript.slice(startIdx, endIdx);
    let proactiveDetectedInBatch = false;
    for (const msg of newMessages) {
      const text = typeof msg?.text === "string" ? msg.text : "";
      const isSystem = msg?.role === "assistant" && text.startsWith("[");

      if (isProactiveTranscriptMessage(msg)) {
        const proactiveFingerprint = text.toLowerCase();
        if (!proactiveFingerprintRef.current.has(proactiveFingerprint)) {
          proactiveFingerprintRef.current.add(proactiveFingerprint);
          proactiveDetectedInBatch = true;
          pushAlert("Aether Insight: proactive guidance available.");
        }
      }

      if (!isSystem) continue;

      const reminderAlert = getReminderAlertMessage(text);
      if (!reminderAlert) continue;

      const fingerprint = reminderAlert.toLowerCase();
      if (alertFingerprintRef.current.has(fingerprint)) continue;
      alertFingerprintRef.current.add(fingerprint);
      pushAlert(reminderAlert);
    }

    if (proactiveDetectedInBatch) {
      playProactiveChime();
    }
  }, [playProactiveChime, pushAlert, transcript]);

  useEffect(() => {
    if (typeof window === "undefined") return;

    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRecognition) {
      setSpeechSupported(false);
      return;
    }

    const recognition = new SpeechRecognition();
    recognition.lang = "en-US";
    recognition.interimResults = false;
    recognition.maxAlternatives = 1;
    recognition.continuous = false;

    recognition.onstart = () => {
      resultReceivedRef.current = false;
      setListening(true);
      setActionStatus({ loading: true, message: "Listening…" });
    };

    recognition.onerror = () => {
      resultReceivedRef.current = false;
      setListening(false);
      setActionStatus({ ok: false, message: "Voice capture failed" });
      setTimeout(() => setActionStatus(null), 3500);
    };

    recognition.onend = () => {
      setListening(false);
      // If recognition ended without a result (user stopped, timeout, no speech)
      // clear the stale "Listening…" status.
      if (!resultReceivedRef.current) {
        setActionStatus(null);
      }
    };

    recognition.onresult = (event) => {
      resultReceivedRef.current = true;
      const spokenText = event?.results?.[0]?.[0]?.transcript?.trim() ?? "";
      if (!spokenText) {
        setActionStatus({ ok: false, message: "No speech detected" });
        setTimeout(() => setActionStatus(null), 3500);
        return;
      }

      const instruction = extractTimerInstruction(spokenText);
      if (instruction) {
        startTimer(instruction.durationSeconds, instruction.label, { silent: true });
      }

      saveReminder(spokenText, "voice");
    };

    recognitionRef.current = recognition;
    setSpeechSupported(true);

    return () => {
      try {
        recognition.stop();
      } catch {
        // ignored
      }
      recognitionRef.current = null;
    };
  }, [saveReminder, startTimer]);

  const handleVoiceCapture = useCallback(async () => {
    const recognition = recognitionRef.current;
    if (!recognition) {
      setActionStatus({ ok: false, message: "Voice capture not supported in this browser" });
      setTimeout(() => setActionStatus(null), 3500);
      return;
    }

    if (typeof window !== "undefined" && "Notification" in window) {
      if (window.Notification.permission === "default") {
        try {
          await window.Notification.requestPermission();
        } catch {
          // ignored
        }
      }
    }

    try {
      if (listening) {
        recognition.stop();
      } else {
        recognition.start();
      }
    } catch {
      setActionStatus({ ok: false, message: "Microphone is not accessible" });
      setTimeout(() => setActionStatus(null), 3500);
    }
  }, [listening]);

  const clearNeuralStream = useCallback(async () => {
    setActionStatus({ loading: true, message: "Clearing neural stream..." });
    try {
      const res = await fetch("/api/v1/transcript", { method: "DELETE" });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setActionStatus({ ok: false, message: data.detail ?? "Failed to clear stream" });
        setTimeout(() => setActionStatus(null), 3500);
        return;
      }

      setTranscript([]);
      transcriptCountRef.current = 0;
      alertFingerprintRef.current = new Set();
      proactiveFingerprintRef.current = new Set();
      setAlertEvent(null);
      setAlertEvents([]);
      setActionStatus({ ok: true, message: "Neural stream cleared" });
    } catch {
      setActionStatus({ ok: false, message: "Connection failed" });
    }
    setTimeout(() => setActionStatus(null), 3500);
  }, []);

  const resetDemo = useCallback(async () => {
    setDemoBusy(true);
    setActionStatus({ loading: true, message: "Resetting demo state..." });
    try {
      const [forgetRes, clearRes] = await Promise.all([
        fetchJsonWithTimeout("/api/v1/memories/forget", { method: "DELETE" }, 10000),
        fetchJsonWithTimeout("/api/v1/transcript", { method: "DELETE" }, 10000),
      ]);

      if (forgetRes.status !== "success" || clearRes.status !== "success") {
        throw new Error("reset failed");
      }

      setMemories([]);
      setTranscript([]);
      transcriptCountRef.current = 0;
      alertFingerprintRef.current = new Set();
      proactiveFingerprintRef.current = new Set();
      setAlertEvent(null);
      setAlertEvents([]);
      setActionStatus({ ok: true, message: "Demo reset complete" });
    } catch {
      setActionStatus({ ok: false, message: "Demo reset failed" });
    } finally {
      setDemoBusy(false);
      setTimeout(() => setActionStatus(null), 3500);
    }
  }, []);

  const simulateWhisper = useCallback(async () => {
    setDemoBusy(true);
    setActionStatus({ loading: true, message: "Simulating proactive whisper..." });
    try {
      const res = await fetchJsonWithTimeout(
        "/api/v1/trigger/meeting_start",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ attendee_name: "Paul" }),
        },
        10000
      );

      if (res.status !== "success") {
        throw new Error("simulation failed");
      }

      await Promise.all([refreshTranscript(), refreshMemories()]);
      setActionStatus({ ok: true, message: "Proactive whisper injected" });
    } catch {
      setActionStatus({ ok: false, message: "Whisper simulation failed" });
    } finally {
      setDemoBusy(false);
      setTimeout(() => setActionStatus(null), 3500);
    }
  }, [refreshMemories, refreshTranscript]);

  const knowledgeMemories = useMemo(() => {
    const prepared = memories
      .map((item, index) => {
        const text = cleanMemoryText(item?.text);
        return {
          id: item?.id ?? `memory-${index}`,
          text,
          category: item?.category ?? "general",
          timestamp: item?.timestamp ?? new Date().toISOString(),
        };
      })
      .filter((item) => {
        if (!item.text) return false;

        if (isSystemNoiseText(item.text)) return false;
        if (!isReminderLike(item.text)) return false;
        if (!isPrimaryUserMemory(item)) return false;
        return true;
      });

    const dedup = new Map();
    for (const item of prepared) {
      const key = item.text.toLowerCase();
      const existing = dedup.get(key);
      if (!existing) {
        dedup.set(key, item);
        continue;
      }

      // Keep the longest variant to remove partial voice transcripts.
      if (item.text.length > existing.text.length) {
        dedup.set(key, item);
      }
    }

    const values = Array.from(dedup.values());
    const compact = values.filter((candidate) => {
      const candidateLower = candidate.text.toLowerCase();
      return !values.some(
        (other) =>
          other.id !== candidate.id &&
          other.text.length > candidate.text.length + 10 &&
          other.text.toLowerCase().includes(candidateLower)
      );
    });

    return compact.sort(
      (a, b) => new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime()
    );
  }, [memories]);

  return (
    <div className="aurora-bg relative h-full flex flex-col text-zinc-100 overflow-hidden">
      <ActionBar
        actionStatus={actionStatus}
        connected={connected}
        onDemoReset={resetDemo}
        onSimulateWhisper={simulateWhisper}
        demoBusy={demoBusy}
      />
      {!connected && <ConnectionIndicator />}
      {alertEvent && (
        <EventAlertStrip alertEvent={alertEvent} onClose={() => setAlertEvent(null)} />
      )}
      <div className="relative z-10 shrink-0 px-5 pt-4 pb-1">
        <div
          className="inline-flex items-center gap-1 p-1 rounded-2xl"
          style={{
            background:     "rgba(255,255,255,0.03)",
            border:         "1px solid rgba(255,255,255,0.05)",
            backdropFilter: "blur(12px)",
          }}
        >
          {["user", "dashboard"].map((tab) => (
            <button
              key={tab}
              onClick={() => setActiveTab(tab)}
              className="px-4 py-1.5 rounded-xl text-xs font-semibold tracking-wide transition-all duration-200"
              style={{
                color: activeTab === tab ? "rgba(255,255,255,0.92)" : "rgba(113,113,122,1)",
                background: activeTab === tab
                  ? "linear-gradient(135deg,rgba(99,102,241,0.28) 0%,rgba(139,92,246,0.18) 100%)"
                  : "transparent",
                border: activeTab === tab
                  ? "1px solid rgba(99,102,241,0.32)"
                  : "1px solid transparent",
                boxShadow: activeTab === tab
                  ? "0 0 14px rgba(99,102,241,0.15)"
                  : "none",
              }}
            >
              {tab === "user" ? "Interface" : "Dashboard"}
            </button>
          ))}
        </div>
      </div>

      {activeTab === "user" ? (
        <main className="relative z-10 flex-1 px-4 pb-4 overflow-hidden min-h-0">
          <UserTab
            connected={connected}
            listening={listening}
            speechSupported={speechSupported}
            onVoiceCapture={handleVoiceCapture}
            memories={knowledgeMemories}
          />
        </main>
      ) : (
        <main className="relative z-10 flex flex-1 flex-col lg:flex-row gap-3 p-4 overflow-hidden min-h-0">
          <section className="flex-1 min-w-0 min-h-0 flex flex-col gap-3">
            <TranscriptPanel messages={transcript} onClear={clearNeuralStream} />
            <AlertCenter alerts={alertEvents} />
          </section>
          <MemoriesPanel memories={knowledgeMemories} />
        </main>
      )}
    </div>
  );
}
