/* ============================================================
   KOULAM × 770 — le scroll fait entrer dans la synagogue
   ============================================================ */
(function () {
  'use strict';

  gsap.registerPlugin(ScrollTrigger);

  var reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  var lenis = null;
  if (!reduceMotion && typeof Lenis !== 'undefined') {
    lenis = new Lenis({ lerp: 0.09, wheelMultiplier: 0.95 });
    window.__lenis = lenis;
    lenis.on('scroll', ScrollTrigger.update);
    gsap.ticker.add(function (t) { lenis.raf(t * 1000); });
    gsap.ticker.lagSmoothing(0);
  }

  var loader = document.getElementById('loader');
  window.addEventListener('load', function () {
    setTimeout(function () { loader.classList.add('is-done'); }, 600);
  });
  setTimeout(function () { loader.classList.add('is-done'); }, 2600);

  /* portrait → séquence 9:16, paysage → 16:9 ; un flip d'orientation recharge */
  var portrait = window.matchMedia('(orientation: portrait)').matches;
  var DIR = portrait ? 'img/seq916' : 'img/seq169';
  window.matchMedia('(orientation: portrait)').addEventListener('change', function () {
    location.reload();
  });

  var exp = document.getElementById('exp');
  var canvas = document.getElementById('seqCanvas');

  fetch(DIR + '/manifest.json')
    .then(function (r) { if (!r.ok) throw 0; return r.json(); })
    .then(function (m) { setup(m.count, m.pad, m.ext, m.veilC, m.veilW, m.v); })
    .catch(function () { /* frames absentes : page statique */ });

  function setup(COUNT, PAD, EXT, VEIL_C, VEIL_W, VER) {
    var ctx = canvas.getContext('2d');
    var frames = new Array(COUNT);
    var loaded = new Array(COUNT);
    var current = 0, target = 0, drawnFrame = -1;

    function src(i) {
      var n = String(i + 1); while (n.length < (PAD || 4)) n = '0' + n;
      /* jeton de version : les frames gardent le même nom d'une refonte à
         l'autre et sont servies en max-age 14400. Sans ça, un visiteur qui
         a déjà vu le site garde les anciennes images pendant 4 h. */
      return DIR + '/f_' + n + '.' + (EXT || 'jpg') + (VER ? '?v=' + VER : '');
    }
    function load(i, cb) {
      if (frames[i]) return;
      var im = new Image();
      im.onload = function () { loaded[i] = true; if (cb) cb(); };
      im.src = src(i);
      frames[i] = im;
    }

    load(0, function () { drawnFrame = -1; });
    var q = 1;
    (function pump() {
      var batch = 0;
      while (q < COUNT && batch < 6) { load(q); q++; batch++; }
      if (q < COUNT) setTimeout(pump, 110);
    })();

    function nearestLoaded(i) {
      if (loaded[i]) return i;
      for (var d = 1; d < COUNT; d++) {
        if (i - d >= 0 && loaded[i - d]) return i - d;
        if (i + d < COUNT && loaded[i + d]) return i + d;
      }
      return -1;
    }

    function resize() {
      var dpr = Math.min(window.devicePixelRatio || 1, 2);
      canvas.width = canvas.clientWidth * dpr;
      canvas.height = canvas.clientHeight * dpr;
      drawnFrame = -1;
    }
    window.addEventListener('resize', resize);
    resize();

    function draw(i) {
      var im = frames[i];
      if (!im || !loaded[i]) return;
      var cw = canvas.width, ch = canvas.height;
      var ir = im.naturalWidth / im.naturalHeight, cr = cw / ch;
      var dw, dh, dx, dy;
      if (ir > cr) { dh = ch; dw = ch * ir; dx = (cw - dw) / 2; dy = 0; }
      else { dw = cw; dh = cw / ir; dx = 0; dy = (ch - dh) / 2; }
      ctx.drawImage(im, dx, dy, dw, dh);
      drawnFrame = i;
    }

    gsap.ticker.add(function () {
      current += (target - current) * 0.22;
      var i = nearestLoaded(Math.round(current));
      if (i !== -1 && i !== drawnFrame) draw(i);
    });

    var hero = document.getElementById('hero');
    var finale = document.getElementById('finale');
    var veil = document.getElementById('veil');
    var fill = document.getElementById('pbFill');
    var caps = document.querySelectorAll('.cap');
    var RANGES = [[0.18, 0.42], [0.48, 0.72]];

    ScrollTrigger.create({
      trigger: exp,
      start: 'top top',
      end: 'bottom bottom',
      onUpdate: function (self) {
        var p = self.progress;

        /* la séquence occupe 0 → 0.88, on tient la dernière frame ensuite */
        var f = Math.min(1, p / 0.88) * (COUNT - 1);
        target = Math.max(0, Math.min(COUNT - 1, f));

        if (fill) fill.style.transform = 'scaleX(' + p + ')';

        /* voile noir centré sur le franchissement de la porte */
        if (veil && typeof VEIL_C === 'number') {
          var w = VEIL_W || 0.06;
          veil.style.opacity = Math.max(0, 1 - Math.abs(p - VEIL_C) / w);
        }

        if (hero) {
          var o = Math.max(0, 1 - p * 8);
          hero.style.opacity = o;
          hero.style.pointerEvents = o < 0.4 ? 'none' : '';
        }

        caps.forEach(function (c, i) {
          var r = RANGES[i];
          c.classList.toggle('is-on', p >= r[0] && p <= r[1]);
        });

        finale.classList.toggle('is-on', p > 0.86);
        document.querySelector('.pin').classList.toggle('is-finale', p > 0.86);
      }
    });
  }
})();
