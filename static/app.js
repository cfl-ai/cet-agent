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

// 页面提示：微信环境不支持内置 TTS
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

// ==================== 统一 TTS 播放入口 ====================
let _currentAudio = null;   // 复用 audio 对象

async function speakText(text, lang = 'en-US', rate = 0.9) {
  if (!text) return;

  // ★ 策略 1：桌面浏览器 → 用 speechSynthesis（零延迟）
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

  // ★ 策略 2：移动端/微信 → 用服务端 TTS
  await speakViaServer(text, lang);
}

async function speakViaServer(text, lang) {
  // 显示加载提示
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

    // 用 HTMLAudioElement 播放（手机浏览器兼容性好）
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
        // iOS 浏览器需要用户手势，提供手动播放提示
        if (AUDIO_SUPPORT.isIOS) {
          showToast('⚠️ iOS 需点击页面后才能播放，请点击"🔊 重播"', 4000);
          // 提供重播按钮
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
  // 移除旧按钮
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
  // 5秒后自动移除
  setTimeout(() => btn.remove(), 5000);
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

  if (!confirm(`确定开始 ${year}年${month}月 ${level.toUpperCase()} 第${setNum}套真题演练？`)) return;

  area.innerHTML = '<div class="loading">准备试卷中...</div>';
  try {
    const startResp = await fetch('/api/exam/paper/start', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({user_id: USER_ID, level, year, month, set_number: setNum})
    });
    const startData = await startResp.json();
    if (startData.code !== 0) throw new Error(startData.detail);
    paperSession = startData.data;

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
  area.innerHTML = `<div class="timer" id="${timerId}">⏱️ ${formatTime(remaining)}</div>`;

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

  await fetch('/api/exam/paper/submit', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({session_id: paperSession.session_id, answers: userAnswers})
  });

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
        <h3>📋 真题演练结束</h3>
        <p>客观题：${g.correct}/${g.objective_total || g.total}，
           正确率 ${(g.accuracy*100).toFixed(0)}%</p>
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
  const modelLabel = {mlp: "🤖 MLP模型", heuristic: "📐 启发式"}[review.prediction_model] || "";

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
      <h3>🎯 备考建议 <span style="font-size:12px;color:#a0aec0;">(${modelLabel})</span></h3>
      <p style="font-size:14px;line-height:1.7;">${review.suggestion}</p>
    </div>
  `;
}

// ==================== 6. 智能助手 ====================
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
      const msgs = (d.messages || []).filter(m => m).slice(-5);
      const intent = d.intent || 'unknown';
      box.insertAdjacentHTML('beforeend',
        `<div class="chat-msg sys">🎯 识别意图：${intent} | 迭代 ${d.iterations} 轮</div>`);
      const summary = msgs.length
        ? msgs.join('\n')
        : (d.result?.content || JSON.stringify(d.result || {}).slice(0, 300));
      box.insertAdjacentHTML('beforeend',
        `<div class="chat-msg bot">${escapeHtml(summary)}</div>`);
    }
  } catch (e) {
    box.querySelectorAll('.chat-msg.sys').forEach(el => el.remove());
    box.insertAdjacentHTML('beforeend',
      `<div class="chat-msg bot">❌ ${escapeHtml(e.message)}</div>`);
  }
  box.scrollTop = box.scrollHeight;
}

function escapeHtml(s) {
  return String(s || '').replace(/[&<>"']/g, c => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]
  ));
}

// ==================== 7. AI工具箱 ====================
async function doResearch() {
  const q = document.getElementById('research-input').value.trim();
  if (!q) return;
  const area = document.getElementById('research-result');
  area.innerHTML = '<div class="loading">检索中...</div>';
  const resp = await fetch('/api/research?query=' + encodeURIComponent(q), {method: 'POST'});
  const data = await resp.json();
  area.innerHTML = data.code === 0
    ? `<div style="padding:12px;background:#f7fafc;border-radius:8px;">${data.data.summary}</div>`
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
      ${escapeHtml(r.content.slice(0, 200))}
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
    ? `<div style="padding:12px;background:#f7fafc;border-radius:8px;">${data.translation}</div>`
    : '<p style="color:#f56565;">翻译失败</p>';
}

// 预加载语音列表
if (window.speechSynthesis) {
  window.speechSynthesis.getVoices();
  window.speechSynthesis.onvoiceschanged = () => window.speechSynthesis.getVoices();
}
// ==================== 学情预测与学习方案 ====================
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
        <strong>水平定位：</strong>${escapeHtml(p.analysis.current_level)}
      </p>
      <p style="font-size:14px;line-height:1.7;margin-top:8px;">
        <strong>差距分析：</strong>${escapeHtml(p.analysis.gap_analysis)}
      </p>
      <div style="margin-top:12px;">
        <strong style="font-size:14px;">关键问题：</strong>
        ${(p.analysis.key_issues || []).map((issue, i) => `
          <div style="padding:8px 12px;background:#fff5f5;border-radius:6px;margin-top:8px;font-size:13px;">
            <strong>${i+1}.</strong> ${escapeHtml(issue)}
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
            ${i+1}. ${escapeHtml(m.name)}
          </h4>
          <p style="font-size:13px;color:#4a5568;line-height:1.7;">
            <strong>为什么适合你：</strong>${escapeHtml(m.why)}
          </p>
          <p style="font-size:13px;color:#4a5568;line-height:1.7;margin-top:6px;">
            <strong>具体操作：</strong>${escapeHtml(m.how)}
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
            ${escapeHtml(t.topic)}
          </p>
          <p style="font-size:13px;color:#4a5568;line-height:1.7;">
            ${escapeHtml(t.content)}
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
                ${escapeHtml(phase.name)}
              </h4>
              <span class="badge" style="font-size:12px;">
                ${phase.duration_days} 天
              </span>
            </div>
            <p style="font-size:13px;color:#4a5568;margin-top:6px;">
              <strong>重点：</strong>${escapeHtml(phase.focus || '')}
            </p>
            ${(phase.daily_tasks || []).length ? `
              <div style="margin-top:10px;">
                <strong style="font-size:13px;">每日任务：</strong>
                ${phase.daily_tasks.map(t => `
                  <div style="padding:8px 12px;background:white;border-radius:6px;margin-top:8px;font-size:13px;">
                    <div style="display:flex;justify-content:space-between;flex-wrap:wrap;">
                      <strong>${escapeHtml(t.task)}</strong>
                      <span style="color:#3182ce;">${escapeHtml(t.amount)}</span>
                    </div>
                    <p style="color:#718096;margin-top:4px;font-size:12px;">
                      目标：${escapeHtml(t.goal || '')}
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
            ${escapeHtml(m.target)}
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
  text += `水平定位：${p.analysis.current_level}\n`;
  text += `差距分析：${p.analysis.gap_analysis}\n`;
  text += `关键问题：\n${(p.analysis.key_issues || []).map((x,i)=>`  ${i+1}. ${x}`).join('\n')}\n\n`;

  text += '【推荐学习方法】\n';
  (p.methods || []).forEach((m, i) => {
    text += `${i+1}. ${m.name}\n   为什么：${m.why}\n   怎么做：${m.how}\n`;
  });

  text += '\n【应试技巧】\n';
  (p.tips || []).forEach(t => {
    text += `· ${t.topic}：${t.content}\n`;
  });

  text += '\n【分阶段计划】\n';
  ['phase_1', 'phase_2', 'phase_3'].forEach(key => {
    const phase = p.study_plan && p.study_plan[key];
    if (!phase) return;
    text += `\n▶ ${phase.name}（${phase.duration_days}天）\n`;
    text += `  重点：${phase.focus}\n`;
    (phase.daily_tasks || []).forEach(t => {
      text += `  · ${t.task} | ${t.amount} | 目标：${t.goal}\n`;
    });
  });

  text += '\n【里程碑】\n';
  (p.milestones || []).forEach(m => {
    text += `第 ${m.day} 天：${m.target}\n`;
  });

  navigator.clipboard.writeText(text).then(() => {
    showToast('✅ 方案已复制到剪贴板', 2000);
  }).catch(() => {
    alert('复制失败，请手动选择文本');
  });
}