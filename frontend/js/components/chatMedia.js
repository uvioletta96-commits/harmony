/**
 * Chat attachments: photos, video, voice messages and voice circles.
 *
 * Recording is `MediaRecorder`, which every current browser has and which needs no
 * permission beyond the microphone. Two decisions are worth stating because they
 * are the ones that are easy to get wrong:
 *
 *   - The container is whatever the browser produces and is handed over unexamined.
 *     WebM/Opus on Firefox and Chrome, MP4/AAC on Safari and iOS. The server sniffs
 *     the bytes and stores what they are; the client renders what the server says.
 *     Picking a format here would mean shipping an encoder to the browser.
 *
 *   - A voice message is recorded by holding the button, and a circle by the same
 *     gesture with the mode toggled. Holding is right because it means the mic is
 *     only open while a finger is down: there is no recording that continues after
 *     the reader has let go, and therefore nothing to send by accident.
 *
 * The waveform is computed here, from the recorded audio, because the server
 * cannot decode the file to draw it and a player with no waveform is a plain
 * progress bar with extra steps.
 */

import { t } from '../core/i18n.js';

import api from '../core/api.js';
import { el } from '../core/dom.js';
import { icon } from '../core/icons.js';
import toast from '../core/toast.js';

/** Longest one recording may run, matching the server's own cap. */
const MAX_SECONDS = 300;

/** How many peaks the waveform carries. One bar per entry when drawn. */
const PEAKS = 48;

/**
 * Pick a container the browser will actually produce.
 *
 * Asked rather than assumed: Safari and iOS have no `audio/webm`, and asking for
 * one there produces a recorder that records nothing at all, silently.
 */
function preferredMimeType() {
  if (typeof MediaRecorder === 'undefined') return '';
  const candidates = [
    'audio/webm;codecs=opus',
    'audio/webm',
    'audio/mp4;codecs=mp4a.40.2',
    'audio/mp4',
    'audio/ogg;codecs=opus',
  ];
  for (const type of candidates) {
    try {
      if (MediaRecorder.isTypeSupported(type)) return type;
    } catch {
      /* an engine that throws on isTypeSupported supports nothing we asked for */
    }
  }
  return '';
}

/** Why recording is unavailable, or null when it is available. */
export function recordBlocker() {
  if (typeof MediaRecorder === 'undefined') {
    return t('Браузер не умеет записывать голосовые');
  }
  // The rule that actually bites in practice, and the reason the button appeared to
  // be broken rather than locked.
  //
  // `navigator.mediaDevices` is only exposed in a *secure context*. The site is
  // served over plain HTTP on an IP address, so the whole object is `undefined` -
  // not the method, the object. Measured on the deployed site:
  //
  //     isSecureContext                false
  //     typeof navigator.mediaDevices  "undefined"
  //     typeof MediaRecorder           "function"
  //
  // So the encoder exists and the microphone does not, and "does your browser
  // support recording" is the wrong question to put to the reader. No amount of
  // feature detection finds a way around it: this is a browser security rule, and
  // the only fix is HTTPS.
  if (!window.isSecureContext) {
    return t('Голосовые работают только по HTTPS — сейчас сайт открыт без него');
  }
  if (!navigator.mediaDevices?.getUserMedia) {
    return t('Браузер не умеет записывать голосовые');
  }
  return null;
}

/** Whether this browser can record at all. */
export function canRecord() {
  return recordBlocker() === null;
}

/**
 * Peaks for the waveform, 0-100.
 *
 * Read from the decoded audio rather than guessed from the file size: a waveform
 * that does not match the recording is worse than none, because the reader watches
 * it to see whether the message is worth playing.
 *
 * Downmixed to mono first - a stereo recording averaged across channels is the
 * difference between a waveform that looks like the sound and one that looks like
 * noise.
 */
async function computeWaveform(blob) {
  const fallback = Array.from({ length: PEAKS }, () => 40);
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return fallback;

    const bytes = await blob.arrayBuffer();
    const context = new Ctx();
    try {
      const buffer = await context.decodeAudioData(bytes);
      const channel = buffer.getChannelData(0);
      const step = Math.max(1, Math.floor(channel.length / PEAKS));
      const peaks = [];
      for (let index = 0; index < PEAKS; index += 1) {
        let total = 0;
        let count = 0;
        for (let offset = 0; offset < step && index * step + offset < channel.length; offset += 1) {
          total += Math.abs(channel[index * step + offset]);
          count += 1;
        }
        peaks.push(count ? Math.round((total / count) * 100) : 0);
      }
      // Normalised against the loudest peak: a quiet recording should still draw a
      // readable shape rather than a flat line.
      const loudest = Math.max(...peaks, 1);
      return peaks.map((peak) => Math.round((peak / loudest) * 100));
    } finally {
      // Chrome keeps an AudioContext alive until it is closed, and a leaked one per
      // recording is a leaked one per message.
      context.close?.();
    }
  } catch {
    // A container the browser recorded but cannot decode - Safari writing MP4 is
    // the common case - still plays; it just cannot be drawn.
    return fallback;
  }
}

/**
 * Milliseconds, for the length shown on the bubble.
 *
 * Measured by the wall clock rather than read from the file, and deliberately so:
 * a `MediaRecorder` blob carries no duration header the browser will hand back
 * without a decode, and the number only has to be close enough to draw a progress
 * bar. The audio element corrects it once metadata loads.
 */
function durationOf(elapsedMs) {
  return elapsedMs;
}

/**
 * Record one clip.
 *
 * Returns a handle with `stop()`, which resolves to the recorded data or null if
 * the reader cancelled. Nothing is uploaded from here - the caller decides when,
 * because a recording is worthless until it is attached to a message.
 */
export async function startRecording({ onLevel, onTick } = {}) {
  const blocked = recordBlocker();
  if (blocked) {
    // Thrown with the reason attached so the composer can show the real one instead
    // of flattening every cause into "your browser cannot do this".
    const error = new Error(blocked);
    error.blocked = true;
    throw error;
  }

  const stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      echoCancellation: true,
      noiseSuppression: true,
    },
  });

  const mimeType = preferredMimeType();
  const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
  const chunks = [];

  recorder.addEventListener('dataavailable', (event) => {
    if (event.data && event.data.size) chunks.push(event.data);
  });

  const startedAt = Date.now();
  let stopped = false;

  // A live level meter, so the reader can see the microphone is actually picking
  // them up. A silent recording that then gets sent is the worst outcome here.
  let audioContext = null;
  let levelTimer = 0;
  if (onLevel && window.AudioContext) {
    try {
      audioContext = new AudioContext();
      const source = audioContext.createMediaStreamSource(stream);
      const analyser = audioContext.createAnalyser();
      analyser.fftSize = 512;
      source.connect(analyser);
      const samples = new Uint8Array(analyser.frequencyBinCount);
      levelTimer = setInterval(() => {
        analyser.getByteTimeDomainData(samples);
        let peak = 0;
        for (const sample of samples) peak = Math.max(peak, Math.abs(sample - 128));
        onLevel(Math.min(100, Math.round((peak / 128) * 160)));
      }, 90);
    } catch {
      // The meter is a nicety; the recording does not depend on it.
    }
  }

  let tickTimer = 0;
  if (onTick) {
    tickTimer = setInterval(() => {
      const seconds = Math.floor((Date.now() - startedAt) / 1000);
      onTick(seconds);
      // Stop at the cap rather than telling the reader afterwards: a recording the
      // server will refuse is worse than one that stopped cleanly.
      if (seconds >= MAX_SECONDS) handle.stop();
    }, 250);
  }

  recorder.start(120);

  function cleanup() {
    stopped = true;
    clearInterval(levelTimer);
    clearInterval(tickTimer);
    audioContext?.close?.();
    for (const track of stream.getTracks()) track.stop();
  }

  const handle = {
    startedAt,

    get elapsedMs() {
      return Date.now() - startedAt;
    },

    get atLimit() {
      return (Date.now() - startedAt) / 1000 >= MAX_SECONDS;
    },

    /**
     * Stop and resolve to the recording, or null if there was nothing to send.
     *
     * Resolves rather than rejects for the cancelled case: releasing the button
     * early is a decision, not an error, and the caller should simply have nothing
     * to upload.
     */
    stop() {
      if (stopped) return Promise.resolve(null);
      return new Promise((resolve) => {
        const elapsed = Date.now() - startedAt;
        recorder.addEventListener('stop', async () => {
          cleanup();
          if (!chunks.length || elapsed < 400) {
            // Under 400ms is a mis-tap, not a message. Sending it produces a
            // bubble the reader cannot hear anything in.
            resolve(null);
            return;
          }
          const blob = new Blob(chunks, { type: recorder.mimeType || mimeType || 'audio/webm' });
          const waveform = await computeWaveform(blob);
          resolve({
            blob,
            waveform,
            durationMs: durationOf(elapsed),
          });
        }, { once: true });
        recorder.stop();
      });
    },

    cancel() {
      cleanup();
      try {
        recorder.stop();
      } catch {
        /* already stopped */
      }
    },
  };

  return handle;
}

/** Upload one recording, tagged as a voice message or a circle. */
export async function uploadRecording(conversationId, recording, kind = 'voice') {
  const body = new FormData();
  body.append('file', recording.blob, kind === 'circle' ? 'circle.webm' : 'voice.webm');
  body.append('kind', kind);
  body.append('duration_ms', String(Math.round(recording.durationMs)));
  body.append('waveform', JSON.stringify(recording.waveform || []));

  const result = await api.upload(`/conversations/${conversationId}/media`, body);
  return result.files[0];
}

/** Upload a photo or a video picked from the file system. */
export async function uploadFile(conversationId, file, kind) {
  const body = new FormData();
  body.append('file', file);
  body.append('kind', kind);

  const result = await api.upload(`/conversations/${conversationId}/media`, body);
  return result.files[0];
}

/** Pick files and return them already uploaded. */
export function pickAndUpload(conversationId, accept, kind) {
  return new Promise((resolve) => {
    const input = el('input', { type: 'file', accept, multiple: true, class: 'visually-hidden' });
    document.body.append(input);

    input.addEventListener('change', async () => {
      const files = [...(input.files || [])];
      input.remove();
      if (!files.length) {
        resolve([]);
        return;
      }
      const uploaded = [];
      for (const file of files) {
        try {
          uploaded.push(await uploadFile(conversationId, file, kind));
        } catch (error) {
          toast.error(error.message);
        }
      }
      resolve(uploaded);
    });

    // Cancelling the dialog fires no event in most browsers, so the promise would
    // never settle. Removing the node on the next focus is the usual workaround.
    window.addEventListener('focus', () => {
      setTimeout(() => {
        if (document.body.contains(input) && !(input.files || []).length) {
          input.remove();
          resolve([]);
        }
      }, 400);
    }, { once: true });

    input.click();
  });
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------


function formatDuration(ms) {
  const total = Math.max(0, Math.round((Number(ms) || 0) / 1000));
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${minutes}:${String(seconds).padStart(2, '0')}`;
}

/**
 * A voice message: play/pause, a waveform, and the elapsed time.
 *
 * The bars are drawn from the peaks the recorder captured and are drawn *filled*
 * up to the playhead, so the reader can see where they are without a separate
 * progress element.
 */
export function voiceMessage(attachment) {
  const audio = el('audio', {
    src: attachment.url,
    preload: 'metadata',
    'aria-label': t('Голосовое сообщение'),
  });

  const peaks = (attachment.waveform && attachment.waveform.length)
    ? attachment.waveform
    : Array.from({ length: PEAKS }, () => 40);

  const bars = peaks.map((peak) => el('span', {
    class: 'voice-bar',
    style: { height: `${Math.max(8, Math.min(100, peak))}%` },
  }));

  const waveform = el('span', { class: 'voice-wave', 'aria-hidden': 'true' }, ...bars);
  const time = el('span', { class: 'voice-time', text: formatDuration(attachment.duration_ms) });

  const playButton = el('button', {
    class: 'voice-play',
    type: 'button',
    'aria-label': t('Воспроизвести'),
    title: t('Воспроизвести'),
  }, icon('play', { size: 18 }));

  const node = el('div', { class: 'voice-note' }, playButton, waveform, time);

  const paint = () => {
    const ratio = audio.duration ? audio.currentTime / audio.duration : 0;
    bars.forEach((bar, index) => {
      bar.classList.toggle('is-played', index / bars.length <= ratio);
    });
    time.textContent = audio.duration
      ? formatDuration(audio.currentTime * 1000)
      : formatDuration(attachment.duration_ms);
  };

  playButton.addEventListener('click', () => {
    if (audio.paused) {
      // One voice message at a time, the same rule the vertical feed follows: three
      // overlapping recordings on a phone is noise with no way to tell which to mute.
      for (const other of document.querySelectorAll('audio.chat-audio')) {
        if (other !== audio) other.pause();
      }
      audio.play().catch(() => toast.error(t('Не удалось воспроизвести')));
      node.classList.add('is-playing');
      playButton.replaceChildren(icon('pause', { size: 18 }));
      playButton.setAttribute('aria-label', t('Пауза'));
    } else {
      audio.pause();
      node.classList.remove('is-playing');
      playButton.replaceChildren(icon('play', { size: 18 }));
      playButton.setAttribute('aria-label', t('Воспроизвести'));
    }
  });

  audio.className = 'chat-audio';
  audio.addEventListener('timeupdate', paint);
  audio.addEventListener('ended', () => {
    node.classList.remove('is-playing');
    playButton.replaceChildren(icon('play', { size: 18 }));
    playButton.setAttribute('aria-label', t('Воспроизвести'));
    paint();
  });
  audio.addEventListener('loadedmetadata', paint);

  node.append(audio);
  return node;
}

/**
 * A voice circle: the same recording, drawn as the sender's avatar.
 *
 * A circle is a voice message with nowhere to put a waveform, so the length is
 * shown as a ring around the avatar instead - an arc the reader can read without a
 * legend.
 */
export function voiceCircle(attachment, sender) {
  const audio = el('audio', {
    src: attachment.url,
    preload: 'metadata',
    'aria-label': t('Голосовое сообщение'),
  });
  audio.className = 'chat-audio';

  const ring = el('span', { class: 'circle-ring', 'aria-hidden': 'true' });
  const playIcon = icon('play', { size: 24 });
  const face = el('span', { class: 'circle-face' },
    sender?.avatar_url
      ? el('img', { src: sender.avatar_url, alt: '', class: 'circle-avatar' })
      : el('span', { class: 'circle-initials', text: sender?.initials || '?' }),
    playIcon,
  );

  const node = el('button', {
    class: 'voice-circle',
    type: 'button',
    'aria-label': t('Голосовое сообщение, {v0}', { v0: formatDuration(attachment.duration_ms) }),
    title: formatDuration(attachment.duration_ms),
  }, ring, face);

  const paint = () => {
    const ratio = audio.duration ? audio.currentTime / audio.duration : 0;
    ring.style.setProperty('--played', `${Math.round(ratio * 100)}%`);
  };

  node.addEventListener('click', () => {
    if (audio.paused) {
      for (const other of document.querySelectorAll('audio.chat-audio')) {
        if (other !== audio) other.pause();
      }
      audio.play().catch(() => toast.error(t('Не удалось воспроизвести')));
      node.classList.add('is-playing');
    } else {
      audio.pause();
      node.classList.remove('is-playing');
    }
    paint();
  });
  audio.addEventListener('timeupdate', paint);
  audio.addEventListener('ended', () => {
    node.classList.remove('is-playing');
    paint();
  });
  audio.addEventListener('loadedmetadata', paint);

  node.append(audio);
  return node;
}

/** One photo or video inside a message. */
export function mediaItem(attachment, { onOpen } = {}) {
  if (attachment.kind === 'video') {
    const video = el('video', {
      class: 'bubble-video',
      src: attachment.url,
      poster: attachment.thumbnail_url || undefined,
      controls: '',
      playsinline: '',
      preload: 'metadata',
      'aria-label': attachment.alt_text || t('Видео'),
    });
    video.addEventListener('play', () => {
      for (const other of document.querySelectorAll('video.bubble-video')) {
        if (other !== video) other.pause();
      }
    });
    return video;
  }

  // Lazy here, unlike in the vertical feed, and the difference is the scroller.
  //
  // The feed's problem was a nested *snap* scroller, whose contents the browser's
  // viewport estimate does not account for: a photo the reader was looking at stayed
  // at naturalWidth 0. This is an ordinary overflow container inside a normal
  // document flow, so what is below its fold really is below the viewport and lazy
  // loading defers it correctly. A long thread is dozens of photos, and decoding all
  // of them is the difference between scrolling and waiting.
  const image = el('img', {
    class: 'bubble-image',
    src: attachment.url,
    alt: attachment.alt_text || '',
    loading: 'lazy',
    decoding: 'async',
  });
  if (onOpen) {
    image.addEventListener('click', () => onOpen(attachment));
    image.classList.add('is-zoomable');
  }
  return image;
}

/**
 * Every attachment on one message.
 *
 * A voice message or circle stands alone in its bubble - a bar beside a sentence is
 * two things to read, and the recording is the message. Photos and video sit above
 * any caption, which is the order the reader expects.
 */
export function messageAttachments(message, { onOpen } = {}) {
  const attachments = message.attachments || [];
  if (!attachments.length) return null;

  const voice = attachments.filter((a) => a.kind === 'voice' || a.kind === 'circle');
  const visual = attachments.filter((a) => a.kind !== 'voice' && a.kind !== 'circle');

  const nodes = [];

  if (voice.length) {
    nodes.push(el('div', { class: 'bubble-voice' },
      ...voice.map((attachment) => (
        attachment.kind === 'circle'
          ? voiceCircle(attachment, message.sender)
          : voiceMessage(attachment)
      )),
    ));
  }

  if (visual.length) {
    const grid = el('div', {
      class: `bubble-media bubble-media-${Math.min(visual.length, 3)}`,
    }, ...visual.map((attachment) => mediaItem(attachment, { onOpen })));
    nodes.push(grid);
  }

  return el('div', { class: 'bubble-attachments' }, ...nodes);
}