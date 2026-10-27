const USER_ID = localStorage.getItem('user_id') || (() => {
  const id = 'user_' + Math.random().toString(36).slice(2, 10);
  localStorage.setItem('user_id', id);
  return id;
})();

// ==================== 环境检测 ====================
const AUDIO_SUPPORT = (() => {
  const ua = navigator.userAgent;
  const isWechat = /MicroMessenger/i.test(ua);
  const isIOS = /iPhone|iPad|iPod/i.test(ua);
  const isAndroid = /Android/i.test(ua);
  const isMobile = isIOS || isAndroid;
  const isHTTPS = location.protocol === 'https:';
  const hasSpeech = typeof window.speechSynthesis !== 'undefined';
  let voiceCount = 0;
  if (hasSpeech) {
    try { voiceCount = window.speechSynthesis.getVoices().length; } catch (e) {}
  }
  return { isWechat, isIOS, isAndroid, isMobile, isHTTPS, hasSpeech, voiceCount };
})();

console.log('🔊 音频环境:', AUDIO_SUPPORT);

// 微信环境提示
if (AUDIO_SUPPORT.isWechat) {
  document.addEventListener('DOMContentLoaded', () => {
    const bar = document.getElementById('status-bar');
    if (bar) {
      const tip = document.createElement('div');
      tip.style.cssText = 'width:100%;padding:6px 0;color:#c05621;font-size:12px;';
      tip.textContent = '⚠️ 微信内不支持浏览器内置语音，将自动使用服务端语音。';
      bar.appendChild(tip);
    }
  });
}

document.querySelectorAll('.tab').forEach(tab => {
  tab.onclick = () => {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById(tab.dataset.tab).classList.add('active');
  };
});

let currentQuestions = [];
let userAnswers = {};
let paperSession = null;
let paperTimer = null;
let serverTTSAvailable = false;

// ==================== 工具函数 ====================
function escapeHtml(s) {
  return String(s || '').replace(/[&<>"']/g, c => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]
  ));
}

// ★ 安全渲染任意字段（对象/数组/字符串）
function formatField(v) {
  if (v === null || v === undefined) return '';
  if (typeof v === 'string') return v;
  if (typeof v === 'number' || typeof v === 'boolean') return String(v);
  if (Array.isArray(v)) {
    return v.map(item => formatField(item)).join('；');
  }
  if (typeof v === 'object') {
    const labelMap = {
      'frequency': '频率', 'duration': '时长', 'method': '方法',
      'material': '材料', 'steps': '步骤', 'goal': '目标',
      'content': '内容', 'example': '示例', 'task': '任务',
      'amount': '数量', 'time': '时间'
    };
    return Object.entries(v)
      .map(([k, val]) => {
        const label = labelMap[k] || k;
        return `${label}: ${formatField(val)}`;
      })
      .join('；');
  }
  return String(v);
}

// Toast 提示
function showToast(msg, duration = 2000) {
  const t = document.createElement('div');
  t.textContent = msg;
  t.style.cssText = `
    position: fixed; top: 20px; left: 50%; transform: translateX(-50%);
    background: rgba(0,0,0,0.8); color: white; padding: 10px 20px;
    border-radius: 8px; font-size: 14px; z-index: 9999;
    max-width: 80%; text-align: center; word-break: break-word;
  `;
  document.body.appendChild(t);
  if (duration > 0) setTimeout(() => t.remove(), duration);
  return t;
}

function hideToast(t) {
  if (t && t.parentNode) t.remove();
}

// ==================== 健康检查 ====================
async function checkHealth() {
  try {
    const resp = await fetch('/api/health');
    const data = await resp.json();
    const f = data.features || {};
    const dv = document.getElementById('dot-vector');
    const dt = document.getElementById('dot-tracing');
    const dg = document.getElementById('dot-graph');
    const ds = document.getElementById('dot-tts');
    if (dv) dv.className = 'dot ' + (f.vector ? 'on' : 'off');
    if (dt) dt.className = 'dot ' + (f.tracing ? 'on' : 'off');
    if (dg) dg.className = 'dot ' + (f.graph ? 'on' : 'off');
    if (ds) ds.className = 'dot ' + (f.server_tts ? 'on' : 'off');
    serverTTSAvailable = !!f.server_tts;
  } catch (e) {}
}
checkHealth();
setInterval(checkHealth, 30000);

// ==================== TTS ====================
let _currentAudio = null;

async function speakText(text, lang = 'en-US', rate = 0.9) {
  if (!text) return;

  // 桌面浏览器 → 用 speechSynthesis（零延迟）
  if (AUDIO_SUPPORT.hasSpeech && !AUDIO_SUPPORT.isWechat && !AUDIO_SUPPORT.isMobile) {
    try {
      window.speechSynthesis.cancel();
      const utter = new SpeechSynthesisUtterance(text);
      utter.lang = lang;
      utter.rate = rate;
      const voices = window.speechSynthesis.getVoices();
      const target = voices.find(v => v.lang === lang)
                  || voices.find(v => v.lang.startsWith(lang.slice(0, 2)));
      if (target) utter.voice = target;
      window.speechSynthesis.speak(utter);
      return;
    } catch (e) {
      console.warn('speechSynthesis 失败，降级到服务端:', e);
    }
  }

  // 移动端/微信 → 用服务端 TTS
  await speakViaServer(text, lang);
}

async function speakViaServer(text, lang) {
  const toast = showToast('🔊 语音加载中...');

  try {
    const resp = await fetch('/api/tts/audio', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({text, lang, rate: 0.9}),
    });
    const data = await resp.json();
    hideToast(toast);

    if (data.code !== 0 || !data.data || !data.data.url) {
      const msg = (data.data && data.data.message) || '语音服务暂不可用';
      showToast(`❌ ${msg}`, 3000);
      return;
    }

    playAudioUrl(data.data.url);
  } catch (e) {
    hideToast(toast);
    showToast('❌ 语音服务异常：' + e.message, 3000);
  }
}

function playAudioUrl(url) {
  try {
    if (_currentAudio) {
      _currentAudio.pause();
      _currentAudio = null;
    }
    const audio = new Audio(url);
    audio.preload = 'auto';
    _currentAudio = audio;

    const playPromise = audio.play();
    if (playPromise && playPromise.catch) {
      playPromise.catch(err => {
        console.error('音频播放失败:', err);
        if (AUDIO_SUPPORT.isIOS) {
          showToast('⚠️ iOS 需点击页面后才能播放，请点击"🔊 重播"', 4000);
          showReplayButton(url);
        } else {
          showToast('❌ 播放失败：' + err.message, 3000);
        }
      });
    }
  } catch (e) {
    showToast('❌ 播放失败：' + e.message, 3000);
  }
}

function showReplayButton(url) {
  const old = document.getElementById('replay-btn');
  if (old) old.remove();

  const btn = document.createElement('button');
  btn.id = 'replay-btn';
  btn.textContent = '🔊 重播';
  btn.style.cssText = `
    position: fixed; bottom: 80px; right: 20px; z-index: 9999;
    padding: 12px 20px; background: #3182ce; color: white;
    border: none; border-radius: 24px; cursor: pointer;
    font-size: 16px; box-shadow: 0 4px 12px rgba(0,0,0,0.3);
  `;
  btn.onclick = () => {
    playAudioUrl(url);
    btn.remove();
  };
  document.body.appendChild(btn);
  setTimeout(() => btn.remove(), 5000);
}

// ==================== 1. 单词复习 ====================
async function loadVocab() {
  const area = document.getElementById('vocab-area');
  const level = document.getElementById('vocab-level').value;
  const category = document.getElementById('vocab-category').value;
  const count = document.getElementById('vocab-count').value;
  area.innerHTML = '<div class="loading">加载中...</div>';

  let url = `/api/vocab/random?level=${level}&count=${count}`;
  if (category) url += `&category=${encodeURIComponent(category)}`;

  const resp = await fetch(url);
  const data = await resp.json();
  if (!data.words || !data.words.length) {
    area.innerHTML = '<p style="color:#718096;">暂无词汇数据</p>';
    return;
  }
  area.innerHTML = data.words.map(w => `
    <div class="card">
      <h3 style="display:flex;align-items:center;justify-content:space-between;">
        <span>
          <span class="badge">${w.category || '高频'}</span>
          ${w.word}
          <span style="font-size:12px;color:#718096;">${w.phonetic}</span>
        </span>
        <button onclick="speakText('${w.word.replace(/'/g, "\\'")}')"
                style="background:#3182ce;color:white;border:none;border-radius:6px;
                       padding:6px 12px;cursor:pointer;font-size:14px;">🔊</button>
      </h3>
      <p style="font-size:14px;">${w.meaning}</p>
      <p style="font-size:12px;color:#a0aec0;margin-top:6px;">${w.level} · ${w.topic || '通用'}</p>
    </div>
  `).join('');
}

// ==================== 2. 专项训练 ====================
async function startExam() {
  const area = document.getElementById('exam-area');
  area.innerHTML = '<div class="loading">AI出题中...</div>';
  try {
    const resp = await fetch('/api/exam/generate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        num_questions: parseInt(document.getElementById('qcount').value),
        question_type: document.getElementById('qtype').value,
        difficulty: parseInt(document.getElementById('qdiff').value),
      })
    });
    const data = await resp.json();
    if (data.code !== 0) throw new Error(data.detail);
    currentQuestions = data.data.questions;
    userAnswers = {};
    renderQuestions('exam-area');
  } catch (e) {
    area.innerHTML = `<div style="color:#f56565;">出题失败：${e.message}</div>`;
  }
}

async function generateWrongExam() {
  document.querySelector('[data-tab="practice"]').click();
  const area = document.getElementById('exam-area');
  area.innerHTML = '<div class="loading">AI分析错题中...</div>';
  try {
    const resp = await fetch('/api/exam/generate-from-wrong', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({user_id: USER_ID, num: 3})
    });
    const data = await resp.json();
    if (data.code !== 0) throw new Error(data.detail);
    currentQuestions = data.data.questions;
    userAnswers = {};
    renderQuestions('exam-area');
  } catch (e) {
    area.innerHTML = `<div style="color:#f56565;">生成失败：${e.message}</div>`;
  }
}

function renderQuestions(containerId) {
  const area = document.getElementById(containerId);
  area.innerHTML = currentQuestions.map((q, i) => {
    const isListening = (q.question_type || '').startsWith('听力');
    const isWriting = q.question_type === '写作';
    const isTranslation = q.question_type === '翻译-汉译英';
    return `
    <div class="card" data-qidx="${i}">
      <h3>
        <span class="badge">第 ${i+1} 题</span>
        <span class="badge">${q.section || q.question_type}</span>
        ${(q.knowledge_tags || []).map(t => `<span class="badge">${t}</span>`).join('')}
      </h3>
      ${isListening ? `
        <button class="btn secondary" style="margin-bottom:8px;"
                onclick="playQuestion(${i})">🔊 播放听力</button>
      ` : ''}
      <div class="q-content">${q.question}</div>
      ${isWriting ? `
        <textarea class="input" placeholder="在此撰写作文..."
                  onchange="userAnswers[${i}]=this.value"></textarea>
      ` : isTranslation ? `
        <div style="margin-bottom:8px;">
          <button class="btn secondary" onclick="startVoiceInput(${i})">🎤 语音输入</button>
        </div>
        <textarea class="input" placeholder="在此输入英文译文..."
                  onchange="userAnswers[${i}]=this.value"></textarea>
      ` : (q.options || []).map((opt, j) => {
        const letter = String.fromCharCode(65 + j);
        return `<div class="option" data-q="${i}" data-opt="${letter}"
                     onclick="selectOption(${i}, '${letter}')">
                  ${letter}. ${opt}
                </div>`;
      }).join('')}
    </div>`;
  }).join('') + '<button class="btn" onclick="submitExam(\'exam-area\')">提交答案</button>';
}

function playQuestion(idx) {
  const q = currentQuestions[idx];
  const text = q.question.split('\n\n')[0] || q.question;
  speakText(text, 'en-US', 0.85);
}

function selectOption(qIdx, letter) {
  userAnswers[qIdx] = letter;
  document.querySelectorAll(`.option[data-q="${qIdx}"]`).forEach(el => {
    el.classList.remove('selected');
    if (el.dataset.opt === letter) el.classList.add('selected');
  });
}

function startVoiceInput(idx) {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) {
    alert('当前浏览器不支持语音识别。\n请使用：\n• Chrome 桌面版\n• Edge 桌面版\n• 或使用 Android Chrome');
    return;
  }
  const rec = new SR();
  rec.lang = 'en-US';
  rec.interimResults = false;
  rec.onresult = (e) => {
    const text = e.results[0][0].transcript;
    userAnswers[idx] = text;
    const card = document.querySelector(`.card[data-qidx="${idx}"]`);
    const ta = card.querySelector('textarea');
    if (ta) ta.value = text;
    alert('识别结果：' + text);
  };
  rec.onerror = (e) => alert('语音识别失败：' + e.error);
  rec.start();
}

async function submitExam(containerId) {
  const answers = currentQuestions.map((q, i) => ({
    question_id: q.question_id,
    user_answer: userAnswers[i] || ''
  }));
  if (answers.some(a => !a.question_id)) {
    alert('题目ID缺失，请重新生成'); return;
  }

  const resp = await fetch('/api/exam/evaluate', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({user_id: USER_ID, answers})
  });
  const data = await resp.json();
  if (data.code !== 0) { alert('评分失败：' + (data.detail || '')); return; }

  const {grading, review} = data.data;

  currentQuestions.forEach((q, i) => {
    const detail = grading.details.find(d => d.question_id === q.question_id);
    if (!detail) return;

    document.querySelectorAll(`.option[data-q="${i}"]`).forEach(el => {
      el.classList.remove('selected', 'correct', 'wrong');
      if (el.dataset.opt === detail.correct_answer) el.classList.add('correct');
      else if (el.dataset.opt === userAnswers[i] && detail.is_correct === false)
        el.classList.add('wrong');
    });

    const card = document.querySelector(`.card[data-qidx="${i}"]`);
    if (card && !card.querySelector('.answer-reveal')) {
      const reveal = document.createElement('div');
      reveal.className = 'answer-reveal';
      reveal.style.cssText = 'margin-top:12px;padding:12px;background:#f7fafc;border-radius:8px;font-size:13px;line-height:1.7;';

      let label, color;
      if (detail.skipped) { label = '📝 主观题（请对照参考答案自查）'; color = '#4a5568'; }
      else if (detail.is_correct) { label = '✅ 回答正确'; color = '#48bb78'; }
      else { label = '❌ 回答错误'; color = '#f56565'; }

      reveal.innerHTML = `
        <p style="font-weight:600;color:${color};">${label}</p>
        <p style="margin-top:6px;"><strong>正确答案：</strong>${detail.correct_answer || '—'}</p>
        ${detail.explanation ? `<p style="margin-top:6px;color:#4a5568;"><strong>解析：</strong>${detail.explanation}</p>` : ''}
      `;
      card.appendChild(reveal);
    }
  });

  const area = document.getElementById(containerId);
  const objTotal = grading.objective_total || grading.total;
  area.insertAdjacentHTML('beforeend', `
    <div class="card" style="background:#ebf8ff;">
      <h3>📊 本组成绩</h3>
      <p>客观题：<strong>${grading.correct}/${objTotal}</strong>，
         正确率 <strong>${(grading.accuracy*100).toFixed(0)}%</strong></p>
      <p style="margin-top:8px;font-size:13px;">💡 ${review.suggestion || ''}</p>
    </div>
  `);
}

// ==================== 3. 真题演练 ====================
async function startPaper() {
  const area = document.getElementById('paper-area');
  const level = document.getElementById('paper-level').value;
  const year = parseInt(document.getElementById('paper-year').value);
  const month = parseInt(document.getElementById('paper-month').value);
  const setNum = parseInt(document.getElementById('paper-set').value);

  if (!confirm(`确定开始 ${year}年${month}月 ${level.toUpperCase()} 第${setNum}套模考？`)) return;

  area.innerHTML = '<div class="loading">准备试卷中...</div>';
  try {
    const startResp = await fetch('/api/exam/paper/start', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({user_id: USER_ID, level, year, month, set_number: setNum})
    });
    const startData = await startResp.json();
    if (startData.code !== 0) throw new Error(startData.detail);
    paperSession = startData.data;
    const paperMode = startData.data.mode || 'ai_mock';
    if (paperMode === 'ai_mock') {
      showToast('真题答案尚未核验，本次以 AI 模拟卷进行（不计入真题口径）', 3500);
    }

    const paperResp = await fetch('/api/exam/generate', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({num_questions: 4, question_type: 'comprehensive',
                             difficulty: level === 'cet4' ? 3 : 4})
    });
    const paperData = await paperResp.json();
    if (paperData.code !== 0) throw new Error(paperData.detail);
    currentQuestions = paperData.data.questions;
    userAnswers = {};

    renderPaperTimer(paperSession.duration_seconds);
    renderQuestions('paper-area');
  } catch (e) {
    area.innerHTML = `<div style="color:#f56565;">启动失败：${e.message}</div>`;
  }
}

function renderPaperTimer(totalSeconds) {
  let remaining = totalSeconds;
  const area = document.getElementById('paper-area');
  const timerId = 'paper-timer-' + Date.now();
  const modeBadge = paperSession && paperSession.mode === 'ai_mock'
    ? '<span class="badge orange" style="margin-left:8px;">AI 模拟卷</span>'
    : '<span class="badge" style="margin-left:8px;">真题</span>';
  area.innerHTML = `<div class="timer" id="${timerId}">⏱️ ${formatTime(remaining)}</div>`
    + `<p style="font-size:12px;color:#a0aec0;margin:4px 0 8px;">${modeBadge}${
        paperSession && paperSession.notice ? escapeHtml(paperSession.notice) : ''
      }</p>`;

  if (paperTimer) clearInterval(paperTimer);
  paperTimer = setInterval(() => {
    remaining--;
    const el = document.getElementById(timerId);
    if (!el) { clearInterval(paperTimer); return; }
    el.textContent = `⏱️ ${formatTime(remaining)}`;
    if (remaining < 600) el.classList.add('danger');
    else if (remaining < 1800) el.classList.add('warning');
    if (remaining <= 0) {
      clearInterval(paperTimer);
      alert('考试时间到，自动交卷！');
      submitPaper();
    }
  }, 1000);
}

function formatTime(sec) {
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

async function submitPaper() {
  if (!paperSession) return;
  if (paperTimer) clearInterval(paperTimer);

  const answers = currentQuestions.map((q, i) => ({
    question_id: q.question_id, user_answer: userAnswers[i] || ''
  }));

  // ★ 修正：此前提交的是 {0: 'A', 1: 'B'} 这种下标键，后端按 question_id 查不到题，
  //   导致模考分恒为 220（保底）。这里改为 question_id → 作答 的映射。
  const paperAnswers = {};
  currentQuestions.forEach((q, i) => { paperAnswers[q.question_id] = userAnswers[i] || ''; });
  let paperScore = null;
  let paperMeta = null;
  try {
    const subResp = await fetch('/api/exam/paper/submit', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({session_id: paperSession.session_id, answers: paperAnswers})
    });
    const subData = await subResp.json();
    if (subData.code === 0) {
      paperScore = subData.data.score;
      paperMeta = subData.data.score_meta;
    }
  } catch (e) { /* 提交失败不阻断诊断 */ }

  const evalResp = await fetch('/api/exam/evaluate', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({user_id: USER_ID, answers})
  });
  const evalData = await evalResp.json();
  if (evalData.code === 0) {
    const g = evalData.data.grading;
    const area = document.getElementById('paper-area');
    area.insertAdjacentHTML('afterbegin', `
      <div class="card" style="background:#fff5f5;border-color:#fc8181;">
        <h3>📋 模考结束</h3>
        <p>客观题：${g.correct}/${g.objective_total || g.total}，
           正确率 ${(g.accuracy*100).toFixed(0)}%</p>
        ${paperScore != null ? `<p style="margin-top:8px;">折算模考分：<strong>${paperScore}</strong> / 710</p>` : ''}
        ${paperMeta && paperMeta.note ? `<p style="font-size:12px;color:#718096;margin-top:4px;">${escapeHtml(paperMeta.note)}</p>` : ''}
        ${paperMeta && paperMeta.skipped_unverified ? `<p style="font-size:12px;color:#c05621;margin-top:2px;">已跳过 ${paperMeta.skipped_unverified} 道答案未核验题，不计分。</p>` : ''}
      </div>
    `);
  }
  paperSession = null;
}

// ==================== 4. 错题本 ====================
async function loadWrong() {
  const area = document.getElementById('wrong-area');
  area.innerHTML = '<div class="loading">加载中...</div>';
  const resp = await fetch(`/api/wrong/${USER_ID}`);
  const data = await resp.json();
  if (!data.items.length) {
    area.innerHTML = '<p style="color:#718096;">🎉 暂无错题</p>';
    return;
  }
  area.innerHTML = data.items.map(w => `
    <div class="card">
      <h3>错${w.wrong_count}次 · ${(w.knowledge_tags || []).join('、')}</h3>
      <div class="q-content">${w.content}</div>
      <p style="font-size:13px;color:#48bb78;margin-top:8px;">✅ 正确答案：${w.answer}</p>
      <p style="font-size:13px;color:#4a5568;margin-top:6px;">${w.explanation}</p>
    </div>
  `).join('');
}

// ==================== 5. 学情诊断 ====================
async function loadDiagnose() {
  const area = document.getElementById('diagnose-area');
  area.innerHTML = '<div class="loading">分析中...</div>';
  const resp = await fetch('/api/user/' + USER_ID + '/stats');
  const stats = await resp.json();
  const diagResp = await fetch('/api/exam/evaluate', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({user_id: USER_ID, answers: []})
  });
  const diagData = await diagResp.json();
  const review = diagData.code === 0 ? diagData.data.review : null;
  if (!review) {
    area.innerHTML = '<p style="color:#718096;">先完成一些练习再查看诊断</p>';
    return;
  }
  const modelLabel = {mlp: "🤖 MLP模型", heuristic: "📐 启发式(数据不足)"}[review.prediction_model] || "";
  const confLabel = {cold: "❄️ 冷启动", low: "🔰 低", medium: "⚡ 中", high: "🎯 高"}[review.prediction_confidence] || "—";
  const modelInfo = review.prediction_model === "mlp"
    ? `模型 v${review.model_version || 0} · 训练样本 ${review.training_samples || 0} · 误差 ±${(review.model_mae != null ? review.model_mae.toFixed(1) : '—')}分`
    : "尚未有真实成绩数据，预测为启发式估算";

  area.innerHTML = `
    <div class="stat-grid">
      <div class="stat"><div class="value">${stats.total_questions}</div><div class="label">累计答题</div></div>
      <div class="stat"><div class="value">${(stats.accuracy*100).toFixed(0)}%</div><div class="label">正确率</div></div>
      <div class="stat"><div class="value">${stats.unresolved_wrong}</div><div class="label">待复习错题</div></div>
      <div class="stat"><div class="value">${review.predicted_score}</div><div class="label">预测分数</div></div>
      <div class="stat"><div class="value" style="color:${review.pass_probability>0.6?'#48bb78':'#f56565'}">
        ${(review.pass_probability*100).toFixed(0)}%</div><div class="label">过线概率</div></div>
    </div>
    <div class="card">
      <h3>🎯 备考建议 <span style="font-size:12px;color:#a0aec0;">(${modelLabel} · 置信度 ${confLabel})</span></h3>
      <p style="font-size:13px;color:#718096;margin-bottom:8px;">📡 ${modelInfo}</p>
      <p style="font-size:14px;line-height:1.7;">${review.suggestion}</p>
    </div>
  `;
}

// ==================== 6.5 成长档案 ====================
async function loadProfile() {
  const area = document.getElementById('profile-area');
  area.innerHTML = '<div class="loading">加载成长档案...</div>';
  try {
    const resp = await fetch('/api/user/' + USER_ID + '/analytics?days=30');
    const data = await resp.json();
    if (data.code !== 0) throw new Error(data.detail || '加载失败');
    const d = data.data || {};
    const p = d.profile || {};
    const streak = d.streak || {};
    const weak = d.weak_points || [];
    const exams = d.recent_exams || [];
    const trend = d.trend || [];

    const overview = `
      <div class="stat-grid">
        <div class="stat"><div class="value">${p.total_answered || 0}</div><div class="label">累计答题</div></div>
        <div class="stat"><div class="value">${((p.accuracy || 0) * 100).toFixed(0)}%</div><div class="label">总正确率</div></div>
        <div class="stat"><div class="value">${streak.current_streak || 0}</div><div class="label">连续打卡(天)</div></div>
        <div class="stat"><div class="value">${streak.longest_streak || 0}</div><div class="label">最长连续</div></div>
        <div class="stat"><div class="value">${p.active_days || 0}</div><div class="label">累计学习(天)</div></div>
        <div class="stat"><div class="value">${p.open_wrong || 0}</div><div class="label">待复习错题</div></div>
      </div>`;

    const meta = `
      <div class="card" style="background:#f7fafc;">
        <h3>🎯 我的目标</h3>
        <p style="font-size:14px;">备考级别：<strong>${escapeHtml(p.level || 'CET4')}</strong>　
           目标分数：<strong>${p.target_score || '—'}</strong></p>
        <p style="font-size:12px;color:#718096;margin-top:6px;">
          近 7 天答题 ${d.last_7d_questions || 0} 题 · 学习约 ${Math.round((d.last_7d_minutes || 0) / 60)} 小时
        </p>
      </div>`;

    const weakHtml = weak.length ? weak.map(w => {
      const pct = ((w.mastery || 0) * 100).toFixed(0);
      const color = w.mastery >= 0.7 ? '#48bb78' : (w.mastery >= 0.5 ? '#ed8936' : '#f56565');
      const badge = w.open_wrong ? `<span class="badge orange">错题${w.open_wrong}</span>` : '';
      return `
        <div style="margin-top:12px;">
          <div style="display:flex;justify-content:space-between;align-items:center;font-size:13px;">
            <span>${escapeHtml(w.point)} ${badge}</span>
            <span style="color:${color};font-weight:600;">${pct}%</span>
          </div>
          <div style="height:8px;background:#e2e8f0;border-radius:4px;margin-top:4px;overflow:hidden;">
            <div style="width:${pct}%;height:100%;background:${color};"></div>
          </div>
          <p style="font-size:11px;color:#a0aec0;margin-top:4px;">
            练习 ${w.attempts} 次 · 正确 ${w.correct} 次
          </p>
        </div>`;
    }).join('') : '<p style="color:#718096;">暂无薄弱点数据，多做练习即可建立掌握度画像</p>';
    const weakCard = `<div class="card"><h3>📉 薄弱知识点（优先补）</h3>${weakHtml}</div>`;

    const recentTrend = trend.slice(-14);
    const maxQ = Math.max(1, ...recentTrend.map(t => t.questions_done || 0));
    const trendHtml = recentTrend.map(t => {
      const h = Math.round((t.questions_done || 0) / maxQ * 60);
      const label = (t.date || '').slice(5);
      return `
        <div style="flex:1;text-align:center;">
          <div style="display:flex;align-items:flex-end;justify-content:center;height:64px;">
            <div title="${t.questions_done || 0} 题"
                 style="width:72%;background:#3182ce;border-radius:3px 3px 0 0;height:${Math.max(2, h)}px;"></div>
          </div>
          <div style="font-size:9px;color:#a0aec0;margin-top:2px;">${label}</div>
        </div>`;
    }).join('');
    const trendCard = `<div class="card"><h3>📅 近 14 天答题量</h3>
        <div style="display:flex;gap:2px;align-items:flex-end;">${trendHtml}</div></div>`;

    const examHtml = exams.length ? exams.map(e => {
      const used = e.used_seconds ? formatTime(e.used_seconds) : '—';
      const score = e.score ? e.score : '未计分';
      return `<div style="padding:10px 0;border-bottom:1px solid #e2e8f0;
                    font-size:13px;display:flex;justify-content:space-between;">
        <span>${escapeHtml(e.level || '')} ${e.year || ''}.${String(e.month || '').padStart(2, '0')} 第${e.set_number || ''}套</span>
        <span>⏱️ ${used}　📊 ${score}</span>
      </div>`;
    }).join('') : '<p style="color:#718096;">还没有模考记录，去「📋 真题演练」试试</p>';
    const examCard = `<div class="card"><h3>📋 最近模考</h3>${examHtml}</div>`;

    area.innerHTML = overview + meta + weakCard + trendCard + examCard;
    const upd = document.getElementById('profile-updated');
    if (upd) upd.textContent = '更新于 ' +
      new Date((d.generated_at || (Date.now() / 1000)) * 1000).toLocaleString('zh-CN');
  } catch (e) {
    area.innerHTML = `<div style="color:#f56565;">加载失败：${escapeHtml(e.message)}</div>`;
  }
}

async function submitExamScore() {
  const scoreEl = document.getElementById('score-value');
  const score = parseInt(scoreEl.value, 10);
  const level = document.getElementById('score-level').value;
  const date = document.getElementById('score-date').value || null;
  if (!score || score < 0 || score > 710) {
    showToast('请输入 0-710 之间的有效分数', 2500);
    scoreEl.focus();
    return;
  }
  try {
    const resp = await fetch('/api/user/' + USER_ID + '/exam-score', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({score, level, exam_date: date}),
    });
    const data = await resp.json();
    if (data.code !== 0) throw new Error(data.detail || '录入失败');
    showToast('✅ 成绩已录入，AI 将据此持续学习（样本够即自动训练）', 3000);
    loadProfile();
  } catch (e) {
    showToast('❌ ' + e.message, 3000);
  }
}

// ==================== 6. 学情预测与学习方案 ====================
let studyPlanGenerated = false;

async function generateStudyPlan() {
  const area = document.getElementById('plan-area');

  if (studyPlanGenerated) {
    if (!confirm('已有方案，重新生成将消耗 AI 额度。确定继续？')) return;
  }

  const level = document.getElementById('plan-level').value;
  const target = parseInt(document.getElementById('plan-target').value) || 425;
  const days = parseInt(document.getElementById('plan-days').value) || 60;

  area.innerHTML = `
    <div class="card" style="background:#ebf8ff;">
      <div class="loading">
        <p style="font-size:16px;margin-bottom:8px;">🧠 AI 正在分析你的学情数据...</p>
        <p style="font-size:12px;color:#4a5568;">
          收集答题记录 → 分析薄弱知识点 → 生成个性化方案
        </p>
        <p style="font-size:12px;color:#a0aec0;margin-top:8px;">
          通常需要 10-20 秒，请耐心等待
        </p>
      </div>
    </div>
  `;

  try {
    const resp = await fetch('/api/study/predict-plan', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        user_id: USER_ID,
        target_score: target,
        days_left: days,
        level: level,
      })
    });
    const data = await resp.json();
    if (data.code !== 0) throw new Error(data.detail || '生成失败');

    studyPlanGenerated = true;
    renderStudyPlan(area, data.data);
  } catch (e) {
    area.innerHTML = `
      <div class="card" style="background:#fff5f5;">
        <p style="color:#f56565;font-size:14px;">❌ 方案生成失败：${escapeHtml(e.message)}</p>
      </div>
    `;
  }
}

function renderStudyPlan(area, data) {
  if (!data.ready) {
    area.innerHTML = `
      <div class="card" style="background:#fffaf0;">
        <h3>⚠️ 数据不足</h3>
        <p style="font-size:14px;line-height:1.7;">${escapeHtml(data.message)}</p>
        <p style="font-size:13px;color:#718096;margin-top:12px;">
          当前答题记录：<strong>${data.stats.total_questions}</strong> 道
        </p>
      </div>
    `;
    return;
  }

  const s = data.stats;
  const p = data.plan;

  const overview = `
    <div class="card" style="background:linear-gradient(135deg,#ebf8ff,#e6fffa);">
      <h3>📊 学情概览</h3>
      <div class="stat-grid" style="margin-top:12px;">
        <div class="stat"><div class="value">${s.total_questions}</div><div class="label">累计答题</div></div>
        <div class="stat"><div class="value">${(s.overall_accuracy*100).toFixed(0)}%</div><div class="label">整体正确率</div></div>
        <div class="stat"><div class="value">${s.predicted_score}</div><div class="label">预测分数</div></div>
        <div class="stat"><div class="value" style="color:${s.pass_probability>0.6?'#48bb78':'#f56565'}">
          ${(s.pass_probability*100).toFixed(0)}%</div><div class="label">过线概率</div></div>
      </div>
    </div>
  `;

  const analysis = `
    <div class="card">
      <h3>🔍 学情诊断</h3>
      <p style="font-size:14px;line-height:1.7;margin-top:8px;">
        <strong>水平定位：</strong>${escapeHtml(formatField(p.analysis.current_level))}
      </p>
      <p style="font-size:14px;line-height:1.7;margin-top:8px;">
        <strong>差距分析：</strong>${escapeHtml(formatField(p.analysis.gap_analysis))}
      </p>
      <div style="margin-top:12px;">
        <strong style="font-size:14px;">关键问题：</strong>
        ${(p.analysis.key_issues || []).map((issue, i) => `
          <div style="padding:8px 12px;background:#fff5f5;border-radius:6px;margin-top:8px;font-size:13px;">
            <strong>${i+1}.</strong> ${escapeHtml(formatField(issue))}
          </div>
        `).join('')}
      </div>
    </div>
  `;

  const methods = `
    <div class="card">
      <h3>📚 推荐学习方法</h3>
      ${(p.methods || []).map((m, i) => `
        <div style="padding:12px;background:#f7fafc;border-radius:8px;margin-top:12px;">
          <h4 style="font-size:14px;color:#2b6cb0;margin-bottom:8px;">
            ${i+1}. ${escapeHtml(formatField(m.name))}
          </h4>
          <p style="font-size:13px;color:#4a5568;line-height:1.7;">
            <strong>为什么适合你：</strong>${escapeHtml(formatField(m.why))}
          </p>
          <p style="font-size:13px;color:#4a5568;line-height:1.7;margin-top:6px;">
            <strong>具体操作：</strong>${escapeHtml(formatField(m.how))}
          </p>
        </div>
      `).join('')}
    </div>
  `;

  const tips = `
    <div class="card">
      <h3>💡 应试技巧</h3>
      ${(p.tips || []).map(t => `
        <div style="padding:10px 0;border-bottom:1px solid #e2e8f0;">
          <p style="font-size:14px;font-weight:600;color:#2d3748;margin-bottom:4px;">
            ${escapeHtml(formatField(t.topic))}
          </p>
          <p style="font-size:13px;color:#4a5568;line-height:1.7;">
            ${escapeHtml(formatField(t.content))}
          </p>
        </div>
      `).join('')}
    </div>
  `;

  const phases = ['phase_1', 'phase_2', 'phase_3'];
  const phaseColors = ['#ebf8ff', '#f0fff4', '#fffaf0'];
  const studyPlan = `
    <div class="card">
      <h3>📅 分阶段学习计划</h3>
      ${phases.map((key, idx) => {
        const phase = p.study_plan && p.study_plan[key];
        if (!phase) return '';
        return `
          <div style="padding:14px;background:${phaseColors[idx]};border-radius:8px;margin-top:12px;">
            <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;">
              <h4 style="font-size:15px;color:#2d3748;">
                ${escapeHtml(formatField(phase.name))}
              </h4>
              <span class="badge" style="font-size:12px;">
                ${phase.duration_days} 天
              </span>
            </div>
            <p style="font-size:13px;color:#4a5568;margin-top:6px;">
              <strong>重点：</strong>${escapeHtml(formatField(phase.focus || ''))}
            </p>
            ${(phase.daily_tasks || []).length ? `
              <div style="margin-top:10px;">
                <strong style="font-size:13px;">每日任务：</strong>
                ${phase.daily_tasks.map(t => `
                  <div style="padding:8px 12px;background:white;border-radius:6px;margin-top:8px;font-size:13px;">
                    <div style="display:flex;justify-content:space-between;flex-wrap:wrap;">
                      <strong>${escapeHtml(formatField(t.task))}</strong>
                      <span style="color:#3182ce;">${escapeHtml(formatField(t.amount))}</span>
                    </div>
                    <p style="color:#718096;margin-top:4px;font-size:12px;">
                      目标：${escapeHtml(formatField(t.goal || ''))}
                    </p>
                  </div>
                `).join('')}
              </div>
            ` : ''}
          </div>
        `;
      }).join('')}
    </div>
  `;

  const milestones = `
    <div class="card">
      <h3>🎯 量化里程碑</h3>
      ${(p.milestones || []).map(m => `
        <div style="display:flex;align-items:flex-start;padding:10px 0;border-bottom:1px solid #e2e8f0;">
          <div style="min-width:80px;font-weight:700;color:#3182ce;font-size:14px;">
            第 ${m.day} 天
          </div>
          <div style="flex:1;font-size:13px;color:#4a5568;line-height:1.7;">
            ${escapeHtml(formatField(m.target))}
          </div>
        </div>
      `).join('')}
    </div>
  `;

  const actions = `
    <div class="card" style="background:#f7fafc;">
      <div style="display:flex;gap:8px;flex-wrap:wrap;">
        <button class="btn" onclick="generateStudyPlan()">🔄 重新生成</button>
        <button class="btn secondary" onclick="window.print()">🖨️ 打印方案</button>
        <button class="btn secondary" onclick="copyStudyPlan()">📋 复制文本</button>
      </div>
      <p style="font-size:11px;color:#a0aec0;margin-top:8px;">
        生成时间：${new Date(data.generated_at * 1000).toLocaleString('zh-CN')}
      </p>
    </div>
  `;

  area.innerHTML = overview + analysis + methods + tips + studyPlan + milestones + actions;
  window._lastStudyPlan = p;
}

function copyStudyPlan() {
  const p = window._lastStudyPlan;
  if (!p) return;

  let text = '【学情诊断】\n';
  text += `水平定位：${formatField(p.analysis.current_level)}\n`;
  text += `差距分析：${formatField(p.analysis.gap_analysis)}\n`;
  text += `关键问题：\n${(p.analysis.key_issues || []).map((x,i)=>`  ${i+1}. ${formatField(x)}`).join('\n')}\n\n`;

  text += '【推荐学习方法】\n';
  (p.methods || []).forEach((m, i) => {
    text += `${i+1}. ${formatField(m.name)}\n   为什么：${formatField(m.why)}\n   怎么做：${formatField(m.how)}\n`;
  });

  text += '\n【应试技巧】\n';
  (p.tips || []).forEach(t => {
    text += `· ${formatField(t.topic)}：${formatField(t.content)}\n`;
  });

  text += '\n【分阶段计划】\n';
  ['phase_1', 'phase_2', 'phase_3'].forEach(key => {
    const phase = p.study_plan && p.study_plan[key];
    if (!phase) return;
    text += `\n▶ ${formatField(phase.name)}（${phase.duration_days}天）\n`;
    text += `  重点：${formatField(phase.focus)}\n`;
    (phase.daily_tasks || []).forEach(t => {
      text += `  · ${formatField(t.task)} | ${formatField(t.amount)} | 目标：${formatField(t.goal)}\n`;
    });
  });

  text += '\n【里程碑】\n';
  (p.milestones || []).forEach(m => {
    text += `第 ${m.day} 天：${formatField(m.target)}\n`;
  });

  navigator.clipboard.writeText(text).then(() => {
    showToast('✅ 方案已复制到剪贴板', 2000);
  }).catch(() => {
    alert('复制失败，请手动选择文本');
  });
}

// ==================== 7. 智能助手 ====================
async function sendChat() {
  const input = document.getElementById('chat-input');
  const text = input.value.trim();
  if (!text) return;
  input.value = '';

  const box = document.getElementById('chat-box');
  box.insertAdjacentHTML('beforeend',
    `<div class="chat-msg user">${escapeHtml(text)}</div>`);
  box.insertAdjacentHTML('beforeend',
    `<div class="chat-msg sys">🤔 正在规划...</div>`);
  box.scrollTop = box.scrollHeight;

  try {
    const resp = await fetch('/api/agent/run', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({user_request: text, user_id: USER_ID, max_iterations: 3})
    });
    const data = await resp.json();
    box.querySelectorAll('.chat-msg.sys').forEach(el => el.remove());

    if (data.code !== 0) {
      box.insertAdjacentHTML('beforeend',
        `<div class="chat-msg bot">❌ ${escapeHtml(data.detail || '请求失败')}</div>`);
    } else {
      const d = data.data;
      const intent = d.intent || 'unknown';

      // 显示执行过程
      const intentLabel = {
        'generate_question': '📝 生成题目',
        'generate_from_wrong': '🔄 错题变式题',
        'diagnose': '📊 学情诊断',
        'chat': '💬 对话',
        'done': '✅ 完成'
      }[intent] || intent;

      box.insertAdjacentHTML('beforeend',
        `<div class="chat-msg sys">🎯 ${intentLabel} | 共执行 ${d.iterations} 轮</div>`);

      // 显示执行结果
      const summary = (d.messages || []).filter(m => m && String(m).trim()).join('\n');
      if (summary) {
        box.insertAdjacentHTML('beforeend',
          `<div class="chat-msg bot">${escapeHtml(summary)}</div>`);
      } else if (d.result && d.result.content) {
        box.insertAdjacentHTML('beforeend',
          `<div class="chat-msg bot">${escapeHtml(d.result.content)}</div>`);
      }

      // 如果生成了题目，提示用户去"专项训练"查看
      if (intent === 'generate_question' || intent === 'generate_from_wrong') {
        box.insertAdjacentHTML('beforeend',
          `<div class="chat-msg sys">💡 题目已生成，请点击上方「📝 专项训练」查看</div>`);
      }
    }
  } catch (e) {
    box.querySelectorAll('.chat-msg.sys').forEach(el => el.remove());
    box.insertAdjacentHTML('beforeend',
      `<div class="chat-msg bot">❌ ${escapeHtml(e.message)}</div>`);
  }
  box.scrollTop = box.scrollHeight;
}

// ==================== 8. AI工具箱 ====================
async function doResearch() {
  const q = document.getElementById('research-input').value.trim();
  if (!q) return;
  const area = document.getElementById('research-result');
  area.innerHTML = '<div class="loading">检索中...</div>';
  const resp = await fetch('/api/research?query=' + encodeURIComponent(q), {method: 'POST'});
  const data = await resp.json();
  area.innerHTML = data.code === 0
    ? `<div style="padding:12px;background:#f7fafc;border-radius:8px;">${escapeHtml(data.data.summary)}</div>`
    : '<p style="color:#f56565;">检索失败</p>';
}

async function doImage() {
  const p = document.getElementById('image-input').value.trim();
  if (!p) return;
  const area = document.getElementById('image-result');
  area.innerHTML = '<div class="loading">生成中...</div>';
  const resp = await fetch('/api/content/generate', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({prompt: p, content_type: 'image'})
  });
  const data = await resp.json();
  area.innerHTML = data.code === 0
    ? `<img src="${data.data.url}" style="max-width:100%;border-radius:8px;">`
    : '<p style="color:#f56565;">生成失败</p>';
}

async function doSimilarSearch() {
  const q = document.getElementById('similar-input').value.trim();
  if (!q) return;
  const area = document.getElementById('similar-result');
  area.innerHTML = '<div class="loading">检索中...</div>';
  const resp = await fetch('/api/vector/similar', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({query: q, k: 5})
  });
  const data = await resp.json();
  if (data.code !== 0) { area.innerHTML = '<p style="color:#f56565;">检索失败</p>'; return; }
  const items = data.data.results || [];
  if (!items.length) {
    area.innerHTML = '<p style="color:#718096;">未找到相似题</p>';
    return;
  }
  area.innerHTML = items.map((r, i) => `
    <div style="padding:10px;background:#f7fafc;border-radius:6px;margin-bottom:8px;font-size:13px;">
      <strong>#${i+1}</strong> 相似度：${(1 - r.distance).toFixed(3)}<br>
      ${escapeHtml((r.content || '').slice(0, 200))}
    </div>
  `).join('');
}

async function doTranslate() {
  const text = document.getElementById('trans-input').value.trim();
  if (!text) return;
  const area = document.getElementById('trans-result');
  area.innerHTML = '<div class="loading">翻译中...</div>';
  const resp = await fetch('/api/translate', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      text,
      source_lang: document.getElementById('src-lang').value,
      target_lang: document.getElementById('tgt-lang').value
    })
  });
  const data = await resp.json();
  area.innerHTML = data.success
    ? `<div style="padding:12px;background:#f7fafc;border-radius:8px;">${escapeHtml(data.translation)}</div>`
    : '<p style="color:#f56565;">翻译失败</p>';
}

// 预加载语音列表
if (window.speechSynthesis) {
  window.speechSynthesis.getVoices();
  window.speechSynthesis.onvoiceschanged = () => window.speechSynthesis.getVoices();
}
// ==================== 9. AI 写作/翻译批改 ====================
const ERROR_TYPE_LABEL = {
  vocab: '词汇欠缺', careless: '粗心失误', concept: '概念不清',
  knowledge_gap: '知识漏洞', inference: '推理偏差', grammar: '语法薄弱',
};
const GRADE_STATUS_LABEL = {
  pending_review: '待复核', approved: '已复核', overridden: '人工改分',
};

function pct(v) { return Math.round((Number(v) || 0) * 100); }

function scoreColor(score15) {
  const s = Number(score15) || 0;
  if (s >= 11) return '#48bb78';
  if (s >= 8) return '#ed8936';
  return '#f56565';
}

function renderModelMeta(meta) {
  if (!meta) return '';
  const bits = [];
  if (meta.provider) bits.push(meta.provider);
  if (meta.model) bits.push(meta.model);
  if (meta.prompt_version) bits.push(meta.prompt_version);
  if (meta.fallback) bits.push('规则兜底评分');
  if (meta.latency_ms != null) bits.push(Math.round(meta.latency_ms) + 'ms');
  return bits.length ? `<span style="font-size:11px;color:#a0aec0;">${escapeHtml(bits.join(' · '))}</span>` : '';
}

async function gradeWriting() {
  const area = document.getElementById('writing-result');
  const essay = document.getElementById('writing-essay').value.trim();
  const topic = document.getElementById('writing-topic').value.trim();
  const level = document.getElementById('writing-level').value;
  if (!topic) { showToast('请先填写作文题目/要求'); return; }
  if (essay.length < 20) { showToast('作文太短，至少 20 个字符'); return; }
  area.innerHTML = '<div class="loading">🤖 阅卷中：按评分标准打分并逐句批注，约 10-20 秒...</div>';
  try {
    const resp = await fetch('/api/grade/writing', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({user_id: USER_ID, topic, essay, level}),
    });
    const data = await resp.json();
    if (data.code !== 0) throw new Error(data.detail || '批改失败');
    renderWritingGrade(area, data.data);
    loadGradeRecords();
  } catch (e) {
    area.innerHTML = `<div style="color:#f56565;">批改失败：${escapeHtml(e.message)}</div>`;
  }
}

function renderWritingGrade(area, g) {
  const dims = g.dimensions || {};
  const dimRow = (label, v, max) => `
    <div style="display:flex;justify-content:space-between;font-size:13px;margin-top:6px;">
      <span>${label}</span><span>${Number(v || 0).toFixed(1)} / ${max}</span>
    </div>
    <div style="height:6px;background:#e2e8f0;border-radius:3px;overflow:hidden;">
      <div style="width:${pct((v || 0) / max)}%;height:100%;background:${scoreColor(g.score)};"></div>
    </div>`;
  const notes = (g.sentence_notes || []).map(n => `
    <div style="padding:8px 0;border-bottom:1px solid #edf2f7;font-size:13px;">
      <p style="color:#718096;">原句：${escapeHtml(n.sentence || '')}</p>
      <p style="color:#c05621;margin-top:4px;">问题：${escapeHtml(n.issue || '')}</p>
      <p style="color:#2b6cb0;margin-top:2px;">建议：${escapeHtml(n.suggestion || '')}</p>
    </div>`).join('');
  const list = (arr, color) => (arr || []).length
    ? '<ul style="margin:6px 0 0 18px;font-size:13px;line-height:1.8;">' +
      arr.map(x => `<li style="color:${color};">${escapeHtml(formatField(x))}</li>`).join('') + '</ul>'
    : '';
  area.innerHTML = `
    <div class="card" style="background:linear-gradient(135deg,#fffaf0,#f0fff4);">
      <div style="display:flex;align-items:baseline;gap:10px;">
        <div style="font-size:44px;font-weight:700;color:${scoreColor(g.score)};">${Number(g.score || 0).toFixed(1)}</div>
        <div style="font-size:13px;color:#718096;">/ 15 分 · 折算 ${Number(g.score_710 || 0).toFixed(1)} / 106.5<br>
          ${escapeHtml(g.level_band || '')} · ${g.word_count || 0} 词 ·
          <span class="badge">${GRADE_STATUS_LABEL[g.status] || g.status || ''}</span></div>
      </div>
      <p style="font-size:14px;line-height:1.7;margin-top:10px;">${escapeHtml(g.feedback || '')}</p>
      ${dimRow('内容切题', dims.content, 5)}
      ${dimRow('语言运用', dims.language, 5)}
      ${dimRow('组织连贯', dims.organization, 5)}
      <div style="margin-top:12px;">${renderModelMeta(g.model_meta)}
        <span style="font-size:11px;color:#a0aec0;">编号 ${escapeHtml(g.grade_id || '')}</span></div>
    </div>
    ${(g.strengths || []).length ? `<div class="card"><h3>✅ 做得好的地方</h3>${list(g.strengths, '#2f855a')}</div>` : ''}
    ${(g.improvements || []).length ? `<div class="card"><h3>🚀 提分建议</h3>${list(g.improvements, '#c05621')}</div>` : ''}
    ${(g.ai_traces || []).length ? `<div class="card"><h3>🤖 AI/模板痕迹</h3>${list(g.ai_traces, '#805ad5')}</div>` : ''}
    ${notes ? `<div class="card"><h3>🔍 逐句批注</h3>${notes}</div>` : ''}
  `;
}

async function gradeTranslation() {
  const area = document.getElementById('translation-result');
  const source = document.getElementById('translation-source').value.trim();
  const userText = document.getElementById('translation-user').value.trim();
  const ref = document.getElementById('translation-reference').value.trim();
  const level = document.getElementById('translation-level').value;
  if (source.length < 10) { showToast('请先粘贴中文原文'); return; }
  if (userText.length < 10) { showToast('请先填写你的英文译文'); return; }
  area.innerHTML = '<div class="loading">🤖 批改中：逐句比对、找漏译/错译/语法问题，约 10-20 秒...</div>';
  try {
    const resp = await fetch('/api/grade/translation', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({user_id: USER_ID, source_text: source,
                            user_translation: userText, level,
                            reference_translation: ref || null}),
    });
    const data = await resp.json();
    if (data.code !== 0) throw new Error(data.detail || '批改失败');
    renderTranslationGrade(area, data.data);
    loadGradeRecords();
  } catch (e) {
    area.innerHTML = `<div style="color:#f56565;">批改失败：${escapeHtml(e.message)}</div>`;
  }
}

function renderTranslationGrade(area, g) {
  const dims = g.dimensions || {};
  const issues = (g.issues || []).map(it => `
    <div style="padding:8px 0;border-bottom:1px solid #edf2f7;font-size:13px;">
      <span class="badge orange">${escapeHtml(it.type || 'issue')}</span>
      <p style="margin-top:4px;color:#4a5568;">${escapeHtml(it.segment || '')}</p>
      <p style="color:#c05621;margin-top:2px;">${escapeHtml(it.detail || '')}</p>
      <p style="color:#2b6cb0;margin-top:2px;">建议：${escapeHtml(it.suggestion || '')}</p>
    </div>`).join('');
  area.innerHTML = `
    <div class="card" style="background:linear-gradient(135deg,#ebf8ff,#f0fff4);">
      <div style="display:flex;align-items:baseline;gap:10px;">
        <div style="font-size:44px;font-weight:700;color:${scoreColor(g.score)};">${Number(g.score || 0).toFixed(1)}</div>
        <div style="font-size:13px;color:#718096;">/ 15 分 · 折算 ${Number(g.score_710 || 0).toFixed(1)} / 106.5<br>
          ${escapeHtml(g.level_band || '')} ·
          <span class="badge">${GRADE_STATUS_LABEL[g.status] || g.status || ''}</span></div>
      </div>
      <p style="font-size:14px;line-height:1.7;margin-top:10px;">${escapeHtml(g.feedback || '')}</p>
      <div style="display:flex;justify-content:space-between;font-size:13px;margin-top:8px;">
        <span>准确度</span><span>${Number(dims.accuracy || 0).toFixed(1)} / 7.5</span></div>
      <div style="height:6px;background:#e2e8f0;border-radius:3px;overflow:hidden;">
        <div style="width:${pct((dims.accuracy || 0) / 7.5)}%;height:100%;background:${scoreColor(g.score)};"></div></div>
      <div style="display:flex;justify-content:space-between;font-size:13px;margin-top:8px;">
        <span>语言质量</span><span>${Number(dims.language || 0).toFixed(1)} / 7.5</span></div>
      <div style="height:6px;background:#e2e8f0;border-radius:3px;overflow:hidden;">
        <div style="width:${pct((dims.language || 0) / 7.5)}%;height:100%;background:${scoreColor(g.score)};"></div></div>
      <div style="margin-top:12px;">${renderModelMeta(g.model_meta)}</div>
    </div>
    ${issues ? `<div class="card"><h3>🔍 逐句问题</h3>${issues}</div>` : ''}
    ${g.improved_translation ? `<div class="card"><h3>✨ 参考改进译文</h3>
      <p style="font-size:14px;line-height:1.8;">${escapeHtml(g.improved_translation)}</p></div>` : ''}
    ${g.reference_translation ? `<div class="card"><h3>📖 参考译文</h3>
      <p style="font-size:14px;line-height:1.8;">${escapeHtml(g.reference_translation)}</p></div>` : ''}
  `;
}

async function loadGradeRecords() {
  const area = document.getElementById('grade-records');
  if (!area) return;
  area.innerHTML = '<div class="loading">加载中...</div>';
  try {
    const resp = await fetch('/api/grade/records?user_id=' + encodeURIComponent(USER_ID) + '&limit=20');
    const data = await resp.json();
    if (data.code !== 0) throw new Error(data.detail || '加载失败');
    const items = data.data || [];
    if (!items.length) {
      area.innerHTML = '<p style="color:#718096;font-size:13px;">还没有批改记录，先上传一篇作文或译文试试</p>';
      return;
    }
    area.innerHTML = items.map(it => {
      const kind = it.kind === 'translation' ? '🔄 翻译' : '📝 写作';
      const when = it.created_at ? new Date(it.created_at * 1000).toLocaleString('zh-CN') : '';
      return `<div style="padding:10px 0;border-bottom:1px solid #edf2f7;font-size:13px;display:flex;justify-content:space-between;gap:8px;">
        <span>${kind} · <strong style="color:${scoreColor(it.score)};">${Number(it.score || 0).toFixed(1)} 分</strong>
          <span class="badge">${GRADE_STATUS_LABEL[it.status] || it.status || ''}</span></span>
        <span style="color:#a0aec0;">${escapeHtml(when)}</span>
      </div>`;
    }).join('');
  } catch (e) {
    area.innerHTML = `<div style="color:#f56565;font-size:13px;">加载失败：${escapeHtml(e.message)}</div>`;
  }
}

// ==================== 10. 错题归因 ====================
async function analyzeAttribution() {
  const area = document.getElementById('attribution-area');
  area.innerHTML = '<div class="loading">🔎 归因中：结合题型、答题时长、错题次数、掌握度与解析...</div>';
  try {
    const resp = await fetch('/api/wrong/' + encodeURIComponent(USER_ID) + '/attribution', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({use_llm: true, limit: 20}),
    });
    const data = await resp.json();
    if (data.code !== 0) throw new Error(data.detail || '归因失败');
    renderAttribution(area, data.data);
  } catch (e) {
    area.innerHTML = `<div style="color:#f56565;">归因失败：${escapeHtml(e.message)}</div>`;
  }
}

function renderAttribution(area, r) {
  const s = r.summary || {};
  const byType = s.by_error_type || {};
  const typeHtml = Object.keys(byType).length ? Object.entries(byType)
    .map(([k, v]) => `<span class="badge orange" style="margin:2px 4px 2px 0;">${escapeHtml(ERROR_TYPE_LABEL[k] || k)} × ${v}</span>`).join('')
    : '<span style="font-size:13px;color:#718096;">暂无归因数据</span>';
  const weak = (s.top_weak_points || []).map(w => `
    <div style="margin-top:8px;">
      <div style="display:flex;justify-content:space-between;font-size:13px;">
        <span>${escapeHtml(w.knowledge_point || '未标注知识点')}</span>
        <span style="color:#c05621;">错 ${w.wrong_count || 0} 次 · 掌握度 ${pct(w.mastery)}%</span>
      </div>
      <div style="height:6px;background:#e2e8f0;border-radius:3px;margin-top:4px;overflow:hidden;">
        <div style="width:${pct(w.mastery)}%;height:100%;background:#ed8936;"></div>
      </div>
    </div>`).join('');
  const items = (r.items || []).slice(0, 10).map(it => `
    <div class="card">
      <div style="display:flex;justify-content:space-between;font-size:12px;color:#718096;">
        <span>${escapeHtml(it.question_type || '')} · ${escapeHtml(it.knowledge_point || '')}</span>
        <span>置信度 ${pct(it.confidence)}%</span>
      </div>
      <p style="font-size:13px;margin-top:6px;">
        <span class="badge orange">${escapeHtml(ERROR_TYPE_LABEL[it.error_type] || it.error_type || '')}</span>
      </p>
      <p style="font-size:13px;color:#4a5568;margin-top:6px;">根因：${escapeHtml(it.root_cause || '')}</p>
      <p style="font-size:13px;color:#2b6cb0;margin-top:4px;">补救：${escapeHtml(it.remediation || '')}</p>
    </div>`).join('');
  const recs = (r.recommendations || []).length
    ? '<ul style="margin:6px 0 0 18px;font-size:13px;line-height:1.8;">' +
      r.recommendations.map(x => `<li>${escapeHtml(formatField(x))}</li>`).join('') + '</ul>'
    : '';
  area.innerHTML = `
    <div class="card" style="background:linear-gradient(135deg,#fffaf0,#fff5f5);">
      <h3>🔎 错题归因报告</h3>
      <p style="font-size:13px;color:#718096;margin-top:6px;">
        错题 ${s.total_wrong || 0} 道，已归因 ${s.attributed || 0} 道</p>
      <div style="margin-top:8px;">${typeHtml}</div>
      ${weak ? `<div style="margin-top:12px;"><strong style="font-size:13px;">📉 最该优先补的知识点</strong>${weak}</div>` : ''}
      ${recs ? `<div style="margin-top:12px;"><strong style="font-size:13px;">✅ 下一步建议</strong>${recs}</div>` : ''}
    </div>
    ${items}`;
}

// ==================== 11. 个性化提分方案 ====================
async function generateImprovePlan() {
  const area = document.getElementById('improve-area');
  const level = document.getElementById('plan-level').value;
  const target = parseInt(document.getElementById('plan-target').value) || 425;
  const days = parseInt(document.getElementById('plan-days').value) || 60;
  area.innerHTML = '<div class="loading">📈 计算分差、排优先级、生成周计划...</div>';
  try {
    const resp = await fetch('/api/plan/improve', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({user_id: USER_ID, level, target_score: target, days_left: days}),
    });
    const data = await resp.json();
    if (data.code !== 0) throw new Error(data.detail || '生成失败');
    renderImprovePlan(area, data.data);
  } catch (e) {
    area.innerHTML = `<div style="color:#f56565;">生成失败：${escapeHtml(e.message)}</div>`;
  }
}

function renderImprovePlan(area, p) {
  const cur = p.current_score == null ? '—' : Number(p.current_score).toFixed(0);
  const gap = p.gap == null ? '—' : Number(p.gap).toFixed(0);
  const src = p.current_score_source
    || (p.source_snapshot && p.source_snapshot.current_score && p.source_snapshot.current_score.method)
    || 'none';
  const srcLabel = src === 'real_exam' ? '真实考试分'
    : (src === 'heuristic' ? '学情估算（启发式）' : '暂无数据');
  const focus = (p.focus_points || []).map(f => `
    <div style="padding:8px 0;border-bottom:1px solid #edf2f7;font-size:13px;">
      <div style="display:flex;justify-content:space-between;">
        <strong>${escapeHtml(f.knowledge_point || f.point || '')}</strong>
        <span style="color:#c05621;">掌握度 ${pct(f.mastery)}%</span>
      </div>
      <p style="color:#718096;margin-top:2px;">${escapeHtml(f.reason || '')}</p>
    </div>`).join('');
  const weeks = (p.weekly_schedule || []).map(w => `
    <div class="card">
      <h3>第 ${w.week || ''} 周 · ${escapeHtml(w.theme || '')}</h3>
      <ul style="margin:6px 0 0 18px;font-size:13px;line-height:1.8;">
        ${(w.tasks || []).map(t => `<li>${escapeHtml(t.type || '')}：
          <strong>${t.count || ''}</strong> ${escapeHtml(t.unit || '')}
          <span style="color:#a0aec0;">（${escapeHtml(t.reason || '')}）</span></li>`).join('')}
      </ul>
      ${w.milestone ? `<p style="font-size:12px;color:#2f855a;margin-top:6px;">🎯 里程碑：${escapeHtml(w.milestone)}</p>` : ''}
    </div>`).join('');
  const actions = (p.actions || []).length
    ? '<ul style="margin:6px 0 0 18px;font-size:13px;line-height:1.8;">' +
      p.actions.map(a => `<li>${escapeHtml(formatField(a))}</li>`).join('') + '</ul>'
    : '';
  area.innerHTML = `
    <div class="card" style="background:linear-gradient(135deg,#e6fffa,#ebf8ff);">
      <div class="stat-grid">
        <div class="stat"><div class="value">${cur}</div><div class="label">当前分（${srcLabel}）</div></div>
        <div class="stat"><div class="value">${p.target_score || '—'}</div><div class="label">目标分</div></div>
        <div class="stat"><div class="value" style="color:#c05621;">${gap}</div><div class="label">分差</div></div>
        <div class="stat"><div class="value">${p.horizon_days || '—'}</div><div class="label">剩余天数</div></div>
      </div>
      ${p.narrative ? `<p style="font-size:14px;line-height:1.8;margin-top:12px;">${escapeHtml(p.narrative)}</p>` : ''}
      ${p.note ? `<p style="font-size:12px;color:#c05621;margin-top:8px;">⚠️ ${escapeHtml(p.note)}</p>` : ''}
      ${renderModelMeta(p.model_meta)}
    </div>
    ${focus ? `<div class="card"><h3>🎯 优先突破</h3>${focus}</div>` : ''}
    ${weeks}
    ${actions ? `<div class="card"><h3>✅ 每日动作清单</h3>${actions}</div>` : ''}
  `;
}

// 作文词数实时统计
document.addEventListener('DOMContentLoaded', () => {
  const ta = document.getElementById('writing-essay');
  const counter = document.getElementById('writing-count');
  if (ta && counter) {
    const update = () => {
      const words = ta.value.trim().split(/\s+/).filter(Boolean).length;
      counter.textContent = words + ' 词';
    };
    ta.addEventListener('input', update);
    update();
  }
});
