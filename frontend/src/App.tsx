import { useState, useRef, useEffect } from 'react';
import ReactMarkdown from 'react-markdown';

const WS_URL = 'ws://localhost:8000/ws/voice';

declare global {
  interface Window {
    webkitSpeechRecognition: any;
    SpeechRecognition: any;
  }
}

export default function App() {
  const [connected, setConnected] = useState(false);
  const [transcript, setTranscript] = useState<Array<{role: string, text: string}>>([]);
  const [isRecording, setIsRecording] = useState(false);
  const [inputText, setInputText] = useState('');
  const [provider, setProvider] = useState<'local' | 'cloud'>('cloud');
  const [showProviderModal, setShowProviderModal] = useState(false);
  const [pendingProvider, setPendingProvider] = useState<'local' | 'cloud'>('cloud');
  const [disconnecting, setDisconnecting] = useState(false);
  const [disconnectCountdown, setDisconnectCountdown] = useState(0);
  
  const wsRef = useRef<WebSocket | null>(null);
  const audioContextRef = useRef<AudioContext | null>(null);
  const recognitionRef = useRef<any>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [transcript]);

  const connect = () => {
    const sessionId = sessionStorage.getItem('routemaster_session_id');
    const params = new URLSearchParams();
    if (sessionId) params.append('sessionId', sessionId);
    params.append('provider', provider);
    const url = `${WS_URL}?${params.toString()}`;
    const ws = new WebSocket(url);
    
    ws.onopen = () => {
      console.log('Connected to voice server');
      setConnected(true);
    };
    
    ws.onmessage = async (event) => {
      const data = JSON.parse(event.data);
      
      if (data.type === 'session_created') {
        sessionStorage.setItem('routemaster_session_id', data.sessionId);
      } else if (data.type === 'full_history') {
        const formattedHistory = data.history.map((msg: any) => ({
          role: (msg.role === 'model' || msg.role === 'assistant') ? 'assistant' : 'user',
          text: msg.text || ''
        }));
        setTranscript(formattedHistory);
      } else if (data.type === 'transcript') {
        setTranscript(prev => [...prev, { role: data.role === 'model' ? 'assistant' : 'user', text: data.text }]);
      } else if (data.type === 'audio') {
        const audioBytes = atob(data.data);
        const audioArray = new Uint8Array(audioBytes.length);
        for (let i = 0; i < audioBytes.length; i++) {
          audioArray[i] = audioBytes.charCodeAt(i);
        }
        await playAudio(audioArray.buffer);
      } else if (data.type === 'system') {
        setTranscript(prev => [...prev, { role: 'assistant', text: data.message }]);
      } else if (data.type === 'provider_changed') {
        setProvider(data.provider);
      } else if (data.type === 'status') {
        if (data.status === 'processing') {
          if (recognitionRef.current) {
            recognitionRef.current.stop();
            setIsRecording(false);
          }
        } else if (data.status === 'transcription_failed') {
          alert(data.message || 'Could not understand audio. Please try again.');
        }
      } else if (data.type === 'disconnect') {
        console.log('Received disconnect message, delay:', data.delay);
        setDisconnecting(true);
        setDisconnectCountdown(data.delay);
        const interval = setInterval(() => {
          setDisconnectCountdown(prev => {
            if (prev <= 1) {
              console.log('Countdown complete, closing websocket');
              clearInterval(interval);
              wsRef.current?.close();
              sessionStorage.removeItem('routemaster_session_id');
              setConnected(false);
              setTranscript([]);
              setDisconnecting(false);
              return 0;
            }
            return prev - 1;
          });
        }, 1000);
      }
    };
    
    ws.onclose = () => {
      console.log('Disconnected');
      setConnected(false);
    };
    
    wsRef.current = ws;
  };

  const playAudio = async (arrayBuffer: ArrayBuffer) => {
    if (!audioContextRef.current) {
      audioContextRef.current = new AudioContext();
    }
    const ctx = audioContextRef.current;
    const audioBuffer = await ctx.decodeAudioData(arrayBuffer);
    const source = ctx.createBufferSource();
    source.buffer = audioBuffer;
    source.connect(ctx.destination);
    source.onended = () => {
      console.log('Audio playback ended, sending audio_complete');
      wsRef.current?.send(JSON.stringify({ type: 'audio_complete' }));
    };
    source.start();
    console.log('Audio playback started');
  };

  const sendTextMessage = (e?: React.FormEvent) => {
    e?.preventDefault();
    if (!inputText.trim() || !wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
    
    wsRef.current.send(JSON.stringify({ 
      type: 'text', 
      data: inputText.trim() 
    }));
    setInputText('');
  };

  const startRecording = () => {
    const SpeechRecognition = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition;
    if (!SpeechRecognition) {
      alert('Speech recognition not supported in this browser');
      return;
    }
    
    const recognition = new SpeechRecognition();
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.lang = 'en-US';
    
    recognition.onresult = (event: any) => {
      const result = event.results[event.results.length - 1];
      if (result.isFinal) {
        const text = result[0].transcript.trim();
        if (text && wsRef.current?.readyState === WebSocket.OPEN) {
          wsRef.current.send(JSON.stringify({ 
            type: 'text', 
            data: text 
          }));
        }
      }
    };
    
    recognition.onerror = (event: any) => {
      console.error('Speech error:', event.error);
    };
    
    recognition.onend = () => {
      setIsRecording(false);
    };
    
    recognition.start();
    recognitionRef.current = recognition;
    setIsRecording(true);
  };

  const stopRecording = () => {
    if (recognitionRef.current) {
      recognitionRef.current.stop();
      recognitionRef.current = null;
    }
    setIsRecording(false);
  };

  const newChat = () => {
    sessionStorage.removeItem('routemaster_session_id');
    wsRef.current?.close();
    setTranscript([]);
    setConnected(false);
  };

  const handleProviderToggle = () => {
    setPendingProvider(provider === 'local' ? 'cloud' : 'local');
    setShowProviderModal(true);
  };

  const confirmProviderChange = () => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'provider_change',
        provider: pendingProvider
      }));
    }
    setProvider(pendingProvider);
    setShowProviderModal(false);
  };

  return (
    <div className="min-h-screen bg-slate-100 flex flex-col items-center py-8 px-4 font-sans text-slate-800">
      <div className="w-full max-w-3xl flex flex-col h-[90vh]">
        <div className="text-center mb-8">
          <h1 className="text-5xl font-extrabold text-slate-900">
            RouteMaster<span className="text-indigo-600">.ai</span>
          </h1>
          <p className="text-slate-500 mt-2 text-lg">Your AI-Powered Network Support Agent</p>
        </div>
        
        {!connected ? (
          <div className="flex-1 flex flex-col items-center justify-center bg-white rounded-3xl shadow-lg border border-slate-200">
            <h2 className="text-2xl font-bold text-slate-700 mb-4">Get Instant Support</h2>
            <p className="text-slate-500 mb-8 max-w-sm text-center">Connect to our voice-native AI agent for help with your Linksys EA6350 router.</p>
            <button 
              onClick={connect}
              className="bg-indigo-600 hover:bg-indigo-700 transition-all duration-300 ease-in-out shadow-lg shadow-indigo-600/30 text-white py-4 px-12 rounded-full text-xl font-bold transform hover:scale-105"
            >
              Connect to Agent
            </button>
          </div>
        ) : (
          <div className="flex-1 flex flex-col bg-white rounded-3xl shadow-xl overflow-hidden border border-slate-200">
            {/* Header controls */}
            <div className="bg-slate-50 px-6 py-4 border-b border-slate-200 flex justify-between items-center">
              <div className="flex items-center gap-3">
                <div className="relative flex items-center justify-center w-3 h-3">
                  <div className="absolute w-full h-full bg-green-500 rounded-full animate-ping"></div>
                  <div className="w-2 h-2 bg-green-500 rounded-full"></div>
                </div>
                <span className="font-semibold text-slate-700">Connected</span>
              </div>
              <div className="flex items-center gap-4">
                <div className="flex items-center gap-2 bg-slate-100 rounded-full px-3 py-1.5">
                  <span className={`text-xs font-medium ${provider === 'local' ? 'text-indigo-600' : 'text-slate-400'}`}>Groq (Free)</span>
                  <button
                    onClick={handleProviderToggle}
                    className={`relative w-10 h-5 rounded-full transition-colors ${provider === 'local' ? 'bg-indigo-600' : 'bg-slate-400'}`}
                  >
                    <div className={`absolute top-0.5 w-4 h-4 bg-white rounded-full transition-transform ${provider === 'local' ? 'left-0.5' : 'left-5'}`} />
                  </button>
                  <span className={`text-xs font-medium ${provider === 'cloud' ? 'text-indigo-600' : 'text-slate-400'}`}>Gemini (Cloud)</span>
                </div>
                <button 
                  onClick={newChat}
                  className="text-sm font-medium text-slate-500 hover:text-indigo-600 transition-colors"
                >
                  New Chat
                </button>
                <button 
                  onClick={() => wsRef.current?.close()} 
                  className="text-sm font-medium text-slate-500 hover:text-red-500 transition-colors"
                >
                  Disconnect
                </button>
              </div>
            </div>

            {/* Chat Area */}
            <div className="flex-1 overflow-y-auto p-6 space-y-6 bg-slate-100">
              {transcript.length === 0 && (
                <div className="h-full flex items-center justify-center text-slate-400 text-center px-8">
                  <p>The AI agent is ready. You can start by typing or using the voice button.</p>
                </div>
              )}
              {transcript.map((msg, i) => (
                <div key={i} className={`flex items-end gap-3 ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
                  {msg.role !== 'user' && <div className="w-8 h-8 bg-slate-200 rounded-full flex-shrink-0"></div>}
                  <div className={`max-w-[75%] rounded-2xl px-5 py-3 shadow-md ${
                    msg.role === 'user' 
                      ? 'bg-indigo-600 text-white rounded-br-lg' 
                      : 'bg-white text-slate-800 rounded-bl-lg border border-slate-200'
                  }`}>
                    {msg.role !== 'user' && <p className="text-xs font-bold text-indigo-500 mb-1">RouteMaster AI</p>}
                    <div className="prose prose-slate max-w-none">
                      <ReactMarkdown>{msg.text}</ReactMarkdown>
                    </div>
                  </div>
                </div>
              ))}
              <div ref={messagesEndRef} />
            </div>

            {/* Input Area */}
            <div className="p-4 bg-white border-t border-slate-200">
              <form onSubmit={sendTextMessage} className="flex gap-3 items-center">
                <div className="flex-1 relative flex items-center">
                  <input
                    type="text"
                    value={inputText}
                    onChange={(e) => setInputText(e.target.value)}
                    placeholder="Type your message or use the mic..."
                    disabled={disconnecting}
                    className={`flex-1 bg-slate-100 border border-slate-200 rounded-full px-6 py-3 outline-none w-full focus:ring-2 focus:ring-indigo-500 transition-all ${disconnecting ? 'opacity-50 cursor-not-allowed' : ''}`}
                  />
                  <button 
                    type="button"
                    onClick={isRecording ? stopRecording : startRecording}
                    disabled={disconnecting}
                    className={`absolute right-3 p-2 rounded-full transition-all ${disconnecting ? 'opacity-50 cursor-not-allowed' : ''} ${
                      isRecording 
                        ? 'bg-red-500 text-white' 
                        : 'text-slate-400 hover:bg-slate-200 hover:text-slate-600'
                    }`}
                    title={isRecording ? "Stop recording" : "Start voice recording"}
                  >
                    {isRecording ? (
                      <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="currentColor"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>
                    ) : (
                      <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" x2="12" y1="19" y2="22"/></svg>
                    )}
                  </button>
                </div>
                <button 
                  type="submit"
                  disabled={!inputText.trim() || disconnecting}
                  className={`p-3.5 rounded-full flex-shrink-0 transition-all text-white ${
                    inputText.trim() && !disconnecting
                      ? 'bg-indigo-600 hover:bg-indigo-700 shadow-md shadow-indigo-600/30 transform hover:scale-110' 
                      : 'bg-slate-300 cursor-not-allowed'
                  }`}
                >
                  <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><line x1="22" y1="2" x2="11" y2="13"></line><polygon points="22 2 15 22 11 13 2 9 22 2"></polygon></svg>
                </button>
              </form>
            </div>
          </div>
        )}
      </div>

      {showProviderModal && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-white rounded-2xl p-6 max-w-md mx-4 shadow-2xl">
            <h3 className="text-xl font-bold text-slate-800 mb-3">Switch Provider?</h3>
            <p className="text-slate-600 mb-6">
              Switching providers will change the voice, tone, and answers. Your conversation history will be preserved.
            </p>
            <div className="flex gap-3 justify-end">
              <button
                onClick={() => setShowProviderModal(false)}
                className="px-4 py-2 rounded-lg text-slate-600 hover:bg-slate-100 transition-colors"
              >
                Cancel
              </button>
              <button
                onClick={confirmProviderChange}
                className="px-4 py-2 rounded-lg bg-indigo-600 text-white hover:bg-indigo-700 transition-colors"
              >
                Continue
              </button>
            </div>
          </div>
        </div>
      )}

      {disconnecting && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-white rounded-2xl p-8 max-w-sm mx-4 shadow-2xl text-center">
            <div className="w-16 h-16 mx-auto mb-4 rounded-full bg-indigo-100 flex items-center justify-center">
              <span className="text-3xl font-bold text-indigo-600">{disconnectCountdown}</span>
            </div>
            <h3 className="text-xl font-bold text-slate-800 mb-2">Ending session...</h3>
            <p className="text-slate-500">Please wait. A new session will begin when you're ready.</p>
          </div>
        </div>
      )}
    </div>
  );
}