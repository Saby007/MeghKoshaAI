import { FormEvent, KeyboardEvent, useEffect, useRef, useState } from 'react';
import {
  Bot,
  CalendarRange,
  ChevronDown,
  CircleDollarSign,
  Cpu,
  Gauge,
  HardDrive,
  Send,
  ShieldCheck,
  Trash2,
  TrendingUp,
  User,
} from 'lucide-react';
import { askFinOpsChat, type ChatAnswer, type ChatTurn } from '../api';

type ChatMessage =
  | { id: number; role: 'user'; text: string }
  | { id: number; role: 'assistant'; answer: ChatAnswer };

const STARTERS = [
  { label: 'Spend drivers', text: 'Why is subscription X spending more this month?', icon: TrendingUp },
  { label: 'Idle storage', text: 'Show all unattached disks.', icon: HardDrive },
  { label: 'Cost trajectory', text: 'Show the 3, 6, and 12 month trends.', icon: Gauge },
  { label: 'Planning outlook', text: 'What is the expected next-month spend and end-of-year projection?', icon: CalendarRange },
];

function AssistantAnswer({ answer }: { answer: ChatAnswer }) {
  const routed = answer.responseMode === 'model_router';
  return (
    <div className="chat-answer">
      <p className="chat-answer-copy">{answer.answer}</p>
      {answer.metrics.length > 0 && (
        <div className="chat-facts" aria-label="Verified financial facts">
          {answer.metrics.map((metric) => (
            <div key={metric.label}>
              <span>{metric.label}</span>
              <strong>{metric.value}</strong>
              <small>{metric.detail}</small>
            </div>
          ))}
        </div>
      )}
      <details className="chat-evidence" open={answer.resources.length > 0}>
        <summary>
          <span><ShieldCheck size={15} /> Verified evidence</span>
          <span>{answer.resources.length > 0 ? `${answer.resources.length} resources` : `${answer.evidenceKeys.length} references`} <ChevronDown size={14} /></span>
        </summary>
        <div className="chat-evidence-body">
          {answer.resources.length > 0 && (
            <div className="chat-resources">
              {answer.resources.map((resource) => (
                <article key={resource.resourceId}>
                  <span className="chat-resource-icon" aria-hidden="true"><HardDrive size={15} /></span>
                  <span>
                    <strong>{resource.resourceName}</strong>
                    <small>{resource.subscriptionName}</small>
                    <small>{resource.detail}</small>
                  </span>
                  <b>{resource.monthlyCost === null ? 'Cost unavailable' : `${resource.currency} ${resource.monthlyCost.toFixed(2)} / month`}</b>
                </article>
              ))}
            </div>
          )}
          <p>{answer.disclaimer}</p>
        </div>
      </details>
      <footer className="chat-provenance">
        <span className={routed ? 'routed' : 'verified'}>
          {routed ? <Cpu size={13} /> : <ShieldCheck size={13} />}
          {routed ? `Model Router → ${answer.selectedModel ?? 'selected model'}` : 'Verified fallback'}
        </span>
        <span>Snapshot {answer.dataAsOf}</span>
        {answer.usage && answer.usage.totalTokens > 0 && <span>{answer.usage.totalTokens.toLocaleString()} tokens</span>}
      </footer>
    </div>
  );
}

type ChatWindowProps = {
  snapshotId: string | null;
  period: string | null;
  currency: string | null;
  subscriptionCount: number;
  onOpenReport?: () => void;
};

export function ChatWindow({ snapshotId, period, currency, subscriptionCount, onOpenReport }: ChatWindowProps) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [question, setQuestion] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const nextId = useRef(1);
  const requestGeneration = useRef(0);
  const threadEnd = useRef<HTMLDivElement>(null);

  useEffect(() => {
    requestGeneration.current += 1;
    setMessages([]);
    setQuestion('');
    setError(null);
    setLoading(false);
    return () => { requestGeneration.current += 1; };
  }, [snapshotId]);

  useEffect(() => {
    threadEnd.current?.scrollIntoView({ behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'nearest' });
  }, [messages, loading]);

  function conversationHistory(): ChatTurn[] {
    return messages.slice(-6).map((message) => message.role === 'user'
      ? { role: 'user', content: message.text }
      : { role: 'assistant', content: message.answer.answer });
  }

  async function ask(text: string) {
    const trimmed = text.trim();
    if (trimmed.length < 2 || !snapshotId || loading) return;
    const history = conversationHistory();
    const userMessageId = nextId.current++;
    const generation = ++requestGeneration.current;
    setError(null);
    setLoading(true);
    setMessages((current) => [...current, { id: userMessageId, role: 'user', text: trimmed }]);
    setQuestion('');
    try {
      const answer = await askFinOpsChat(trimmed, snapshotId, history);
      if (generation !== requestGeneration.current) return;
      setMessages((current) => [...current, { id: nextId.current++, role: 'assistant', answer }]);
    } catch (askError) {
      if (generation !== requestGeneration.current) return;
      setError(askError instanceof Error ? askError.message : 'The FinOps answer is unavailable.');
      setMessages((current) => current.filter((message) => message.id !== userMessageId));
      setQuestion(trimmed);
    } finally {
      if (generation === requestGeneration.current) setLoading(false);
    }
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    void ask(question);
  }

  function handleComposerKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      void ask(question);
    }
  }

  return (
    <section className="chat-workspace" aria-labelledby="chat-heading">
      <header className="chat-toolbar">
        <span>
          <small>FinOps workspace</small>
          <h1 id="chat-heading">Cost assistant</h1>
        </span>
        <span className="chat-router-state"><Cpu size={14} /> Model Router</span>
        <button type="button" onClick={() => { setMessages([]); setError(null); }} disabled={messages.length === 0 || loading} title="Clear conversation" aria-label="Clear conversation">
          <Trash2 size={16} />
        </button>
      </header>

      {!snapshotId ? (
        <section className="chat-empty">
          <Bot size={28} />
          <h2>No completed report</h2>
          {onOpenReport && <button type="button" className="ghost-button" onClick={onOpenReport}>Open report</button>}
        </section>
      ) : (
        <div className="chat-layout">
          <aside className="chat-context">
            <div className="chat-context-mark"><CircleDollarSign size={18} /></div>
            <span>Report context</span>
            <strong>{period ?? 'Latest period'}</strong>
            <dl>
              <div><dt>Currency</dt><dd>{currency ?? '—'}</dd></div>
              <div><dt>Scope</dt><dd>{subscriptionCount} subscription{subscriptionCount === 1 ? '' : 's'}</dd></div>
              <div><dt>Facts</dt><dd><ShieldCheck size={12} /> Verified</dd></div>
            </dl>
            <nav aria-label="Suggested FinOps questions">
              <span>Ask about</span>
              {STARTERS.map((starter) => {
                const Icon = starter.icon;
                return (
                  <button type="button" key={starter.label} onClick={() => void ask(starter.text)} disabled={loading}>
                    <Icon size={15} />
                    <span><strong>{starter.label}</strong><small>{starter.text}</small></span>
                  </button>
                );
              })}
            </nav>
          </aside>

          <section className="chat-main">
            <section className="chat-thread" aria-label="FinOps conversation" aria-live="polite">
              {messages.length === 0 && (
                <div className="chat-welcome">
                  <span><Bot size={21} /></span>
                  <small>Grounded in {period ?? 'the latest completed period'}</small>
                  <h2>Ask about this report</h2>
                </div>
              )}
              {messages.map((message) => (
                <article className={`chat-message ${message.role}`} key={message.id}>
                  <span className="chat-avatar" aria-hidden="true">
                    {message.role === 'user' ? <User size={15} /> : <Bot size={15} />}
                  </span>
                  {message.role === 'user' ? <p>{message.text}</p> : <AssistantAnswer answer={message.answer} />}
                </article>
              ))}
              {loading && (
                <article className="chat-message assistant chat-thinking" role="status">
                  <span className="chat-avatar" aria-hidden="true"><Bot size={15} /></span>
                  <p><i /><i /><i /> Reviewing verified report evidence</p>
                </article>
              )}
              <div ref={threadEnd} />
            </section>

            <form className="chat-composer" onSubmit={submit}>
              <textarea
                value={question}
                onChange={(event) => setQuestion(event.target.value)}
                onKeyDown={handleComposerKeyDown}
                placeholder="Ask about spend, waste, trends, or forecasts"
                aria-label="Ask about this report"
                rows={2}
                maxLength={500}
                disabled={loading}
              />
              <button type="submit" disabled={loading || question.trim().length < 2} title="Send question" aria-label="Send question">
                <Send size={17} />
              </button>
              {error && <p className="chat-error" role="alert">{error}</p>}
            </form>
          </section>
        </div>
      )}
    </section>
  );
}