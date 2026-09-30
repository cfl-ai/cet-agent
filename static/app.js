const USER_ID = localStorage.getItem('user_id') || (() => {
  const id = 'user_' + Math.random().toString(36).slice(2, 10);
  localStorage.setItem('user_id', id);
  return id;
})();

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
        <button onclick="speakText('${w.word}')"
                style="background:#3182ce;color:white;border:none;border-radius:6px;
                       padding:6px 12px;cursor:pointer;font-size:14px;">🔊</button>
      </h3>
      <p style="font-size:14px;">${w.meaning}</p>
      <p style="font-size:12px;color:#a0aec0;margin-top:6px;">${w.level} · ${w.topic || '通用'}</p>
    </div>
  `).join('');
}

// ==================== TTS ====================
function speakText(text, lang = 'en-US', rate = 0.9) {
  if (!window.speechSynthesis) {
    alert('当前浏览器不支持语音播放');
    return;
  }
  window.speechSynthesis.cancel();
  const utter = new SpeechSynthesisUtterance(text);
  utter.lang = lang;
  utter.rate = rate;
  const voices = window.speechSynthesis.getVoices();
  const target = voices.find(v => v.lang === lang)
              || voices.find(v => v.lang.startsWith(lang.slice(0, 2)));
  if (target) utter.voice = target;
  window.speechSynthesis.speak(utter);
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
          <span style="font-size:12px;color:#a0aec0;margin-left:8px;">
            或直接在下方手写译文
          </span>
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
  // 提取短文部分（去掉题干），按句切分播放
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

// ==================== 语音输入 ====================
function startVoiceInput(idx) {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) { alert('当前浏览器不支持语音识别，请使用 Chrome/Edge'); return; }
  const rec = new SR();
  rec.lang = 'en-US';
  rec.interimResults = false;
  rec.maxAlternatives = 1;
  rec.onresult = (e) => {
    const text = e.results[0][0].transcript;
    userAnswers[idx] = text;
    // 回填到 textarea
    const card = document.querySelector(`.card[data-qidx="${idx}"]`);
    const ta = card.querySelector('textarea');
    if (ta) ta.value = text;
    alert('识别结果：' + text);
  };
  rec.onerror = (e) => alert('语音识别失败：' + e.error);
  rec.start();
}

// ==================== 提交答案 ====================
async function submitExam(containerId) {
  const answers = currentQuestions.map((q, i) => ({
    question_id: q.question_id || `q_${Date.now()}_${i}`,
    user_answer: userAnswers[i] || ''
  }));

  const resp = await fetch('/api/exam/evaluate', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({user_id: USER_ID, answers})
  });
  const data = await resp.json();
  if (data.code !== 0) { alert('评分失败'); return; }

  const {grading, review} = data.data;
  currentQuestions.forEach((q, i) => {
    const detail = grading.details.find(d => d.question_id === q.question_id)
                  || grading.details[i];
    if (!detail) return;
    document.querySelectorAll(`.option[data-q="${i}"]`).forEach(el => {
      el.classList.remove('selected');
      if (el.dataset.opt === detail.correct_answer) el.classList.add('correct');
      else if (el.dataset.opt === userAnswers[i] && !detail.is_correct)
        el.classList.add('wrong');
    });
  });

  const area = document.getElementById(containerId);
  area.insertAdjacentHTML('beforeend', `
    <div class="card" style="background:#ebf8ff;">
      <h3>📊 本组成绩</h3>
      <p>正确 ${grading.correct}/${grading.total}，
         正确率 ${(grading.accuracy*100).toFixed(0)}%</p>
      <p style="margin-top:8px;font-size:13px;">💡 ${review.suggestion}</p>
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

  if (!confirm(`确定开始 ${year}年${month}月 ${level.toUpperCase()} 第${setNum}套真题演练？\n\n计时期间不可暂停，到点自动交卷。`)) return;

  area.innerHTML = '<div class="loading">准备试卷中...</div>';

  try {
    // 1. 启动会话
    const startResp = await fetch('/api/exam/paper/start', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        user_id: USER_ID, level, year, month, set_number: setNum
      })
    });
    const startData = await startResp.json();
    if (startData.code !== 0) throw new Error(startData.detail);
    paperSession = startData.data;

    // 2. 按四六级结构生成试卷题目
    const paperResp = await fetch('/api/exam/generate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        num_questions: 4,
        question_type: 'comprehensive',
        difficulty: level === 'cet4' ? 3 : 4,
      })
    });
    const paperData = await paperResp.json();
    if (paperData.code !== 0) throw new Error(paperData.detail);
    currentQuestions = paperData.data.questions;
    userAnswers = {};

    // 3. 渲染计时器
    renderPaperTimer(paperSession.duration_seconds);
    renderQuestions('paper-area');
    // 4. 定时保存
    setInterval(() => savePaperProgress(), 30000);
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

async function savePaperProgress() {
  if (!paperSession) return;
  await fetch('/api/exam/paper/submit', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      session_id: paperSession.session_id,
      answers: userAnswers
    })
  });
}

async function submitPaper() {
  if (!paperSession) return;
  if (paperTimer) clearInterval(paperTimer);

  const answers = currentQuestions.map((q, i) => ({
    question_id: q.question_id || `paper_${i}`,
    user_answer: userAnswers[i] || ''
  }));

  await fetch('/api/exam/paper/submit', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      session_id: paperSession.session_id,
      answers: userAnswers
    })
  });

  // 评分
  const evalResp = await fetch('/api/exam/evaluate', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({user_id: USER_ID, answers})
  });
  const evalData = await evalResp.json();
  if (evalData.code === 0) {
    const g = evalData.data.grading;
    const area = document.getElementById('paper-area');
    area.insertAdjacentHTML('afterbegin', `
      <div class="card" style="background:#fff5f5;border-color:#fc8181;">
        <h3>📋 真题演练结束</h3>
        <p>正确 ${g.correct}/${g.total}，正确率 ${(g.accuracy*100).toFixed(0)}%</p>
        <p style="font-size:12px;color:#718096;margin-top:8px;">
          会话ID: ${paperSession.session_id}
        </p>
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
      ${(w.options || []).length ? w.options.map((opt, j) =>
        `<div style="font-size:13px;padding:4px 0;">${String.fromCharCode(65+j)}. ${opt}</div>`
      ).join('') : ''}
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
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
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

// ==================== 6. AI工具箱 ====================
async function doResearch() {
  const q = document.getElementById('research-input').value.trim();
  if (!q) return;
  const area = document.getElementById('research-result');
  area.innerHTML = '<div class="loading">检索中...</div>';
  const resp = await fetch('/api/research?query=' + encodeURIComponent(q), {method: 'POST'});
  const data = await resp.json();
  area.innerHTML = data.code === 0
    ? `<div style="padding:12px;background:#f7fafc;border-radius:8px;font-size:14px;">${data.data.summary}</div>`
    : '<p style="color:#f56565;">检索失败</p>';
}

async function doImage() {
  const p = document.getElementById('image-input').value.trim();
  if (!p) return;
  const area = document.getElementById('image-result');
  area.innerHTML = '<div class="loading">生成中...</div>';
  const resp = await fetch('/api/content/generate', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({prompt: p, content_type: 'image'})
  });
  const data = await resp.json();
  area.innerHTML = data.code === 0
    ? `<img src="${data.data.url}" style="max-width:100%;border-radius:8px;">`
    : '<p style="color:#f56565;">生成失败</p>';
}

async function doTranslate() {
  const text = document.getElementById('trans-input').value.trim();
  if (!text) return;
  const area = document.getElementById('trans-result');
  area.innerHTML = '<div class="loading">翻译中...</div>';
  const resp = await fetch('/api/translate', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
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

// 预加载语音
if (window.speechSynthesis) {
  window.speechSynthesis.getVoices();
  window.speechSynthesis.onvoiceschanged = () => window.speechSynthesis.getVoices();
}