/* settings.js — Kiro-style UI settings panel: blur, background, accent, layout */
const ACCENTS = [
  { name: 'Indigo',   hex: '#6366f1', h2: '#818cf8', h3: '#4f46e5' },
  { name: 'Blue',     hex: '#3b82f6', h2: '#60a5fa', h3: '#2563eb' },
  { name: 'Cyan',     hex: '#06b6d4', h2: '#22d3ee', h3: '#0891b2' },
  { name: 'Emerald',  hex: '#10b981', h2: '#34d399', h3: '#059669' },
  { name: 'Green',    hex: '#22c55e', h2: '#4ade80', h3: '#16a34a' },
  { name: 'Amber',    hex: '#f59e0b', h2: '#fbbf24', h3: '#d97706' },
  { name: 'Orange',   hex: '#f97316', h2: '#fb923c', h3: '#ea580c' },
  { name: 'Red',      hex: '#ef4444', h2: '#f87171', h3: '#dc2626' },
  { name: 'Rose',     hex: '#f43f5e', h2: '#fb7185', h3: '#e11d48' },
  { name: 'Magenta',  hex: '#d946ef', h2: '#e879f9', h3: '#c026d3' },
  { name: 'Violet',   hex: '#8b5cf6', h2: '#a78bfa', h3: '#7c3aed' },
  { name: 'Slate',    hex: '#64748b', h2: '#94a3b8', h3: '#475569' },
];

const SETTINGS_KEY = 'phantom_ui_settings';

// Kiro-style preset backgrounds (dark-compatible, subtle patterns/gradients)
const BG_PRESETS = [
  { name: '纯色', url: '' },
  { name: 'Mesh', url: 'https://images.unsplash.com/photo-1558618666-fcd25c85f82e?w=800&q=70' },
  { name: 'Ocean', url: 'https://images.unsplash.com/photo-1507525428034-b723cf961d3e?w=800&q=70' },
  { name: 'Mountain', url: 'https://images.unsplash.com/photo-1519681393784-d120267933ba?w=800&q=70' },
  { name: 'Nord', url: 'https://images.unsplash.com/photo-1534274988757-a28bf1a57c17?w=800&q=70' },
  { name: 'Forest', url: 'https://images.unsplash.com/photo-1441974231531-c6227db76b6e?w=800&q=70' },
  { name: 'Space', url: 'https://images.unsplash.com/photo-1462331940025-496dfbfc7564?w=800&q=70' },
  { name: 'Abstract', url: 'https://images.unsplash.com/photo-1553356084-58ef4a67b2a7?w=800&q=70' },
];

const DEFAULTS = {
  accentIdx: 0,          // indigo
  blur: 12,              // panel blur px (0..30)
  blurOn: true,          // master blur switch
  bubbleBlur: 12,        // message bubble blur px (0..24)
  bubbleTransparency: 52,// bubble transparency % (10..90) — stored as transparency
  assistantTransparency: 40, // AI turn transparency % (5..80)
  bubbleTextColor: '#ffffff', // user bubble text color
  bgBlur: 0,             // background image blur px (0..24)
  glassTransparency: 28, // panel glass transparency % (0..70)
  bgImage: '',           // URL or empty
  overlay: 78,           // overlay depth %
  fontSize: 14,          // px base
  sidebarW: 260,         // px
  transport: 'auto',     // 'poll' | 'ws' | 'auto'
  radius: 10,            // border-radius px
  density: 'normal',     // 'compact' | 'normal' | 'comfortable'
  animate: true,         // animations on/off
  colorScheme: 'dark',   // 'dark' | 'light' | 'auto'
  phantomTheme: 'indigo', // 'indigo' | 'mono' (Fluent black/white/gray)
};

let _settings = {};

function loadSettings() {
  try {
    const raw = localStorage.getItem(SETTINGS_KEY);
    _settings = raw ? { ...DEFAULTS, ...JSON.parse(raw) } : { ...DEFAULTS };
    // Migrate old opacity keys to new transparency keys (one-time)
    if (_settings.bubbleOpacity !== undefined && _settings.bubbleTransparency === DEFAULTS.bubbleTransparency) {
      _settings.bubbleTransparency = Math.round(100 - _settings.bubbleOpacity);
      _settings.assistantTransparency = Math.round(100 - (_settings.assistantOpacity || 60));
      _settings.glassTransparency = Math.round(100 - (_settings.opacity || 72));
      delete _settings.bubbleOpacity;
      delete _settings.assistantOpacity;
      delete _settings.opacity;
      saveSettings();
    }
  } catch (e) {
    _settings = { ...DEFAULTS };
  }
}

function saveSettings() {
  try {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify(_settings));
  } catch (e) {}
}

function renderLowPowerActive() {
  const root = document.documentElement;
  if (!root) return false;
  return root.classList.contains('renderer-low-power') || root.classList.contains('renderer-no-gpu') ||
    root.dataset.renderTier === 'low';
}


function updateSettingsPreview() {
  var pv = document.getElementById("settings-preview");
  if (!pv) return;
  var rs = document.documentElement.style;

  // Overlay mask — top-level tint
  var mask = pv.querySelector(".spv-overlay-mask");
  if (mask) {
    var ov = rs.getPropertyValue("--bg-overlay");
    if (ov) mask.style.setProperty("background", ov, "important");
  }

  // Glass panel
  var glass = pv.querySelector(".spv-glass-panel");
  if (glass) {
    var bv = parseFloat(rs.getPropertyValue("--blur-amount")) || 12;
    glass.style.setProperty("backdrop-filter", "blur(" + (bv * 0.6) + "px) saturate(1.3)", "important");
  }

  // Bubble
  var bubble = pv.querySelector(".spv-bubble");
  if (bubble) {
    var bb = parseFloat(rs.getPropertyValue("--bubble-blur")) || 0;
    bubble.style.setProperty("backdrop-filter", "blur(" + bb + "px) saturate(1.8)", "important");
    var bo = parseFloat(rs.getPropertyValue("--bubble-opacity")) || 48;
    var ac = rs.getPropertyValue("--accent") || "#6366f1";
    bubble.style.setProperty("background", "color-mix(in srgb, " + ac + " " + bo + "%, transparent)", "important");
    bubble.style.setProperty("color", rs.getPropertyValue("--bubble-text-color") || "#ffffff", "important");
  }

  // AI card
  var card = pv.querySelector(".spv-card");
  if (card) {
    var ao = parseFloat(rs.getPropertyValue("--assistant-opacity")) || 0.60;
    var t = rs.getPropertyValue("--text") || "#fafafa";
    card.style.setProperty("background", "linear-gradient(180deg, color-mix(in srgb, " + t + " " + (ao*12).toFixed(1) + "%, transparent), color-mix(in srgb, " + t + " " + (ao*6).toFixed(1) + "%, transparent))", "important");
  }

  // Font label
  var fl = pv.querySelector(".spv-font-label");
  if (fl) { fl.style.setProperty("font-size", document.body.style.fontSize || "14px", "important"); }
}

function applySettings() {
  const s = _settings;
  // Accent — overridden by mono theme
  const a = ACCENTS[s.accentIdx] || ACCENTS[0];
  const root = document.documentElement.style;
  const theme = s.phantomTheme || 'indigo';
  document.body.classList.toggle('theme-mono', theme === 'mono');
  if (theme === 'mono') {
    // Fluent Mono: grayscale palette, glass surfaces, deep shadows
    root.setProperty('--accent', '#8a8a94');
    root.setProperty('--accent2', '#a8a8b0');
    root.setProperty('--accent3', '#6a6a74');
    root.setProperty('--acrylic-tint', 'rgba(20, 20, 24, 0.82)');
    root.setProperty('--acrylic-tint-light', 'rgba(245, 245, 250, 0.82)');
    root.setProperty('--ok', '#5c5c66');
    root.setProperty('--err', '#8a8a94');
    root.setProperty('--run', '#9a9aa4');
  } else {
    root.setProperty('--accent', a.hex);
    root.setProperty('--accent2', a.h2);
    root.setProperty('--accent3', a.h3);
    root.setProperty('--acrylic-tint', 'rgba(30, 30, 35, 0.72)');
    root.setProperty('--acrylic-tint-light', 'rgba(245, 245, 250, 0.72)');
    root.setProperty('--ok', '');
    root.setProperty('--err', '');
    root.setProperty('--run', '');
  }
  // Glass. Low/no-GPU path forces matte surfaces: blur + translucent stacking are the slow/banding path.
  const lowPower = renderLowPowerActive();
  const blurEnabled = !!s.blurOn && !lowPower;
  root.setProperty('--blur-amount', (blurEnabled ? s.blur : 0) + 'px');
  // Transparency → opacity for CSS (higher slider = more transparent)
  const bubbleOpaque = Math.max(10, 100 - s.bubbleTransparency);
  const assistantOpaque = lowPower ? Math.max(82, 100 - s.assistantTransparency) : Math.max(5, 100 - s.assistantTransparency);
  const glassOpaqueBase = Math.max(30, 100 - s.glassTransparency);
  const glassOpaque = lowPower ? Math.max(82, glassOpaqueBase) : glassOpaqueBase;
  root.setProperty('--bubble-blur', (blurEnabled ? s.bubbleBlur : 0) + 'px');
  root.setProperty('--bubble-opacity', bubbleOpaque + '%');
  root.setProperty('--assistant-opacity', (assistantOpaque / 100).toFixed(2));
  root.setProperty('--bubble-text-color', s.bubbleTextColor || '#ffffff');
  root.setProperty('--bg-blur', (lowPower ? 0 : s.bgBlur) + 'px');
  root.setProperty('--glass-opacity', (glassOpaque / 100).toFixed(2));
  document.body.classList.toggle('no-blur', !s.blurOn || lowPower);
  document.body.classList.toggle('auto-low-power', lowPower);
  // Color scheme
  document.body.classList.remove('scheme-dark', 'scheme-light');
  document.body.classList.add('scheme-' + (s.colorScheme || 'dark'));
  if (s.colorScheme === 'auto') {
    document.body.dataset.colorScheme = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  } else {
    document.body.dataset.colorScheme = s.colorScheme;
  }
  const overlayDepth = lowPower ? Math.max(86, s.overlay) : s.overlay;
  const scheme = s.colorScheme === 'auto'
    ? (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')
    : (s.colorScheme || 'dark');
  if (scheme === 'light') {
    root.setProperty('--bg-overlay', `rgba(248,248,252,${((overlayDepth * 0.45) / 100).toFixed(2)})`);
  } else {
    root.setProperty('--bg-overlay', `rgba(12,12,15,${(overlayDepth/100).toFixed(2)})`);
  }
  // Background image
  if (s.bgImage) {
    root.setProperty('--bg-image', `url(${s.bgImage})`);
    document.body.classList.add('has-bg');
  } else {
    root.setProperty('--bg-image', 'none');
    document.body.classList.remove('has-bg');
  }
  // Font size
  document.body.style.fontSize = s.fontSize + 'px';
  // Sidebar width
  root.setProperty('--sidebar-w', s.sidebarW + 'px');
  const side = document.getElementById('side');
  if (side) side.style.width = s.sidebarW + 'px';
  // Border radius
  root.setProperty('--radius', s.radius + 'px');
  root.setProperty('--radius-sm', Math.max(2, s.radius - 4) + 'px');
  root.setProperty('--radius-md', Math.max(4, s.radius - 2) + 'px');
  root.setProperty('--radius-lg', Math.min(24, s.radius + 4) + 'px');
  // Density
  document.body.classList.remove('density-compact', 'density-normal', 'density-comfortable');
  document.body.classList.add('density-' + (s.density || 'normal'));
  // Animations
  document.body.classList.toggle('no-anim', !s.animate || lowPower);
  // Transport
  if (typeof updateTransport === 'function') updateTransport(s.transport);
  // Save accent index separately for HUB compatibility
  try { localStorage.setItem('phantom_accent', String(s.accentIdx)); } catch (e) {}
}

/* ── Settings UI ── */
function initSettingsUI() {
  loadSettings();
  applySettings();
  if (typeof updateSettingsPreview === 'function') updateSettingsPreview();

  // Build accent swatches (clear first to avoid duplicates)
  const swatchDiv = document.getElementById('accent-swatches');
  if (swatchDiv) {
    swatchDiv.innerHTML = '';
    ACCENTS.forEach((a, i) => {
      const btn = document.createElement('button');
      btn.className = 'sswatch' + (i === _settings.accentIdx ? ' on' : '');
      btn.style.background = a.hex;
      btn.title = a.name;
      btn.addEventListener('click', () => {
        _settings.accentIdx = i;
        saveSettings(); applySettings(); if (typeof updateSettingsPreview === 'function') updateSettingsPreview();
        initSettingsUI(); // refresh swatch states
      });
      swatchDiv.appendChild(btn);
    });
  }

  // Bind sliders
  bindSlider('s-blur', 'blur', 'px');
  // Blur toggle button
  const blurOffBtn = document.getElementById('s-blur-off');
  if (blurOffBtn) {
    blurOffBtn.textContent = _settings.blurOn ? '关闭' : '开启';
    blurOffBtn.addEventListener('click', () => {
      _settings.blurOn = !_settings.blurOn;
      blurOffBtn.textContent = _settings.blurOn ? '关闭' : '开启';
      const slider = document.getElementById('s-blur');
      if (slider) slider.disabled = !_settings.blurOn;
      saveSettings(); applySettings();
    });
    const blurSlider = document.getElementById('s-blur');
    if (blurSlider) blurSlider.disabled = !_settings.blurOn;
  }
  bindSlider('s-opacity', 'glassTransparency', '%');
  bindSlider('s-bubble-blur', 'bubbleBlur', 'px');
  bindSlider('s-bubble-opacity', 'bubbleTransparency', '%');
  bindSlider('s-assistant-opacity', 'assistantTransparency', '%');
  bindSlider('s-bg-blur', 'bgBlur', 'px');

  // Bubble text color picker
  const colorEl = document.getElementById('s-bubble-text');
  const colorVal = document.getElementById('s-bubble-text-val');
  if (colorEl) {
    colorEl.value = _settings.bubbleTextColor || '#ffffff';
    colorEl.addEventListener('input', () => {
      _settings.bubbleTextColor = colorEl.value;
      if (colorVal) colorVal.textContent = colorEl.value;
      saveSettings(); applySettings();
    });
  }
  bindSlider('s-overlay', 'overlay', '%');
  bindSlider('s-fontsize', 'fontSize', 'px');
  bindSlider('s-sidebarw', 'sidebarW', 'px');

  // Background URL + preview + presets
  const bgEl = document.getElementById('s-bg');
  const previewEl = document.getElementById('bg-preview');
  const presetsEl = document.getElementById('bg-presets');

  function updateBgPreview() {
    const url = _settings.bgImage || '';
    if (previewEl) {
      previewEl.style.backgroundImage = url ? 'url(' + url + ')' : '';
      previewEl.classList.toggle('has-bg', !!url);
    }
    if (bgEl) bgEl.value = url;
    // Highlight active preset
    if (presetsEl) {
      presetsEl.querySelectorAll('.bg-preset').forEach(btn => {
        const presetUrl = btn.dataset.url || '';
        btn.classList.toggle('on', url === presetUrl && !!url);
      });
    }
  }

  // Build preset thumbnails (clear first to avoid duplicates on re-init)
  if (presetsEl) {
    presetsEl.innerHTML = '';
    BG_PRESETS.forEach(p => {
      const btn = document.createElement('button');
      btn.className = 'bg-preset';
      btn.title = p.name;
      btn.dataset.url = p.url;
      if (p.url) btn.style.backgroundImage = 'url(' + p.url + ')';
      else { btn.textContent = '✕'; btn.style.background = 'var(--panel)'; }
      btn.addEventListener('click', () => {
        _settings.bgImage = p.url;
        saveSettings(); applySettings();
        updateBgPreview();
      });
      presetsEl.appendChild(btn);
    });
  }

  if (bgEl) {
    // Show data URL as a friendly label, not the full base64
    if (_settings.bgImage && _settings.bgImage.startsWith('data:')) {
      bgEl.value = '[本地文件]';
    } else {
      bgEl.value = _settings.bgImage || '';
    }
    bgEl.addEventListener('focus', () => {
      // Clear the friendly label on focus so user can type a URL
      if (bgEl.value === '[本地文件]') bgEl.value = '';
    });
    bgEl.addEventListener('blur', () => {
      if (!bgEl.value.trim() && _settings.bgImage && _settings.bgImage.startsWith('data:')) {
        bgEl.value = '[本地文件]';
      }
    });
    bgEl.addEventListener('input', () => {
      const val = bgEl.value.trim();
      _settings.bgImage = val;
      saveSettings(); applySettings();
      updateBgPreview();
    });
  }
  // File picker for background image
  const bgFileBtn = document.getElementById('s-bg-file');
  const bgFileInput = document.getElementById('s-bg-input');
  if (bgFileBtn && bgFileInput) {
    bgFileBtn.addEventListener('click', () => bgFileInput.click());
    bgFileInput.addEventListener('change', () => {
      const file = bgFileInput.files[0];
      if (!file) return;
      if (file.size > 10 * 1024 * 1024) {
        toast ? toast('图片过大，请选择 10MB 以下的文件') : alert('图片过大，请选择 10MB 以下的文件');
        return;
      }
      // Downscale large images to avoid bloating localStorage
      const img = new Image();
      const url = URL.createObjectURL(file);
      img.onload = () => {
        URL.revokeObjectURL(url);
        let w = img.width, h = img.height;
        const maxDim = 1920;
        if (w > maxDim || h > maxDim) {
          const ratio = Math.min(maxDim / w, maxDim / h);
          w = Math.round(w * ratio); h = Math.round(h * ratio);
        }
        const canvas = document.createElement('canvas');
        canvas.width = w; canvas.height = h;
        canvas.getContext('2d').drawImage(img, 0, 0, w, h);
        const dataUrl = canvas.toDataURL('image/jpeg', 0.75);
        _settings.bgImage = dataUrl;
        saveSettings(); applySettings();
        updateBgPreview();
        if (bgEl) bgEl.value = '[本地文件: ' + file.name + ']';
      };
      img.src = url;
    });
  }

  const bgClear = document.getElementById('s-bg-clear');
  if (bgClear) {
    bgClear.addEventListener('click', () => {
      _settings.bgImage = '';
      if (bgEl) bgEl.value = '';
      saveSettings(); applySettings();
      updateBgPreview();
    });
  }
  updateBgPreview();

  // Transport
  const transSel = document.getElementById('s-transport');
  if (transSel) {
    transSel.value = _settings.transport;
    transSel.addEventListener('change', () => {
      _settings.transport = transSel.value;
      saveSettings(); applySettings();
    });
  }

  // Border radius slider
  bindSlider('s-radius', 'radius', 'px');

  // Density preset buttons
  ['compact', 'normal', 'comfortable'].forEach(d => {
    const btn = document.getElementById('s-density-' + d);
    if (btn) {
      btn.classList.toggle('on', _settings.density === d);
      btn.addEventListener('click', () => {
        _settings.density = d;
        saveSettings(); applySettings(); updateSettingsPreview();
        initSettingsUI();
      });
    }
  });

  // Animation toggle
  const animBtn = document.getElementById('s-animate');
  if (animBtn) {
    animBtn.classList.toggle('on', _settings.animate);
    animBtn.textContent = _settings.animate ? '开启' : '关闭';
    animBtn.addEventListener('click', () => {
      _settings.animate = !_settings.animate;
      animBtn.classList.toggle('on', _settings.animate);
      animBtn.textContent = _settings.animate ? '开启' : '关闭';
      saveSettings(); applySettings();
    });
  }

  // Theme toggle buttons
  ['indigo', 'mono'].forEach(t => {
    const btn = document.getElementById('s-theme-' + t);
    if (btn) {
      btn.classList.toggle('on', _settings.phantomTheme === t);
      btn.addEventListener('click', () => {
        _settings.phantomTheme = t;
        saveSettings(); applySettings(); updateSettingsPreview();
        initSettingsUI(); // refresh accent swatches + theme buttons
      });
    }
  });

  // Color scheme buttons
  ['dark', 'light', 'auto'].forEach(scheme => {
    const btn = document.getElementById('s-scheme-' + scheme);
    if (btn) {
      btn.classList.toggle('on', _settings.colorScheme === scheme);
      btn.addEventListener('click', () => {
        _settings.colorScheme = scheme;
        saveSettings(); applySettings(); updateSettingsPreview();
        initSettingsUI();
      });
    }
  });

  // Reset
  const resetBtn = document.getElementById('settings-reset');
  if (resetBtn) {
    resetBtn.addEventListener('click', () => {
      _settings = { ...DEFAULTS };
      saveSettings(); applySettings();
      initSettingsUI();
    });
  }
}

function bindSlider(elId, key, unit) {
  const slider = document.getElementById(elId);
  const valSpan = document.getElementById(elId + '-val');
  if (!slider) return;
  slider.value = _settings[key];
  if (valSpan) valSpan.textContent = _settings[key] + unit;
  slider.addEventListener('input', () => {
    const v = parseInt(slider.value);
    _settings[key] = v;
    saveSettings(); applySettings();
    if (valSpan) valSpan.textContent = v + unit;
  });
}

function toggleSettings() {
  const ov = document.getElementById('settings-overlay');
  if (ov.classList.contains('hidden')) {
    initSettingsUI();
    ov.classList.remove('hidden');
  } else {
    ov.classList.add('hidden');
  }
}

/* ── Bootstrap ── */
document.addEventListener('DOMContentLoaded', () => {
  loadSettings();
  applySettings();
  // Listen for OS color scheme changes (for 'auto' mode)
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
  if (_settings.colorScheme === 'auto') {
      setTimeout(() => { document.body.dataset.colorScheme = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'; applySettings(); }, 100);
    }
  });

  // Settings button in topbar
  const btn = document.createElement('button');
  btn.id = 'settingsBtn'; btn.textContent = '🎨';
  btn.title = '外观设置';
  btn.addEventListener('click', toggleSettings);
  const topbar = document.getElementById('topbar');
  if (topbar) topbar.appendChild(btn);

  // Close on overlay click
  const ov = document.getElementById('settings-overlay');
  if (ov) {
    ov.addEventListener('click', e => {
      if (e.target === ov) toggleSettings();
    });
    document.getElementById('settings-close')?.addEventListener('click', toggleSettings);
  }

  // Keyboard shortcut: Ctrl+,
  document.addEventListener('keydown', e => {
    if ((e.ctrlKey || e.metaKey) && e.key === ',') {
      e.preventDefault();
      toggleSettings();
    }
  });
});
// v2026-06-25-v2
