/* ============================================================
   Страница «3D-визуализация онтологии» (ontology.html).

   Загружает каталог data/ontologies.json, даёт выбрать опубликованную
   онтологию (или берёт ?id= из ссылки), тянет её граф из file_url
   (data/ontologies/objects/{id}.json) и рисует его в 3D на Canvas 2D:
   перспективная проекция, орбитальное вращение мышью, зум колесом,
   hover/клик по классу — карточка сбоку. Никаких внешних библиотек —
   тот же принцип «чистый JS без сборки», что и весь фронтенд AIOS.

   Узлы — классы (размер по числу связей, цвет по корню иерархии is-a),
   рёбра — отношения (+ светлая линия для parent). 3D-раскладка —
   force-directed: отталкивание вершин, пружины по рёбрам, притяжение
   к центру; симуляция остывает и замирает.
   ============================================================ */

(function () {
  var canvas = document.getElementById('onto3dCanvas');
  if (!canvas) return;

  var wrap = document.getElementById('onto3dWrap');
  var select = document.getElementById('onto3dSelect');
  var metaEl = document.getElementById('onto3dMeta');
  var legendEl = document.getElementById('onto3dLegend');
  var emptyEl = document.getElementById('onto3dEmpty');
  var side = document.getElementById('onto3dSide');
  var sideClose = document.getElementById('onto3dSideClose');
  var cqBox = document.getElementById('onto3dCq');
  var cqList = document.getElementById('onto3dCqList');
  var cqSummary = document.getElementById('onto3dCqSummary');
  var infoEl = document.getElementById('onto3dInfo');
  var infoName = document.getElementById('onto3dInfoName');
  var infoDesc = document.getElementById('onto3dInfoDesc');
  var infoTags = document.getElementById('onto3dInfoTags');
  var infoFacts = document.getElementById('onto3dInfoFacts');
  var addClassBtn = document.getElementById('onto3dAddClass');
  var sideBody = document.getElementById('onto3dSideBody');
  var sideSaveActions = document.getElementById('onto3dSideSaveActions');
  var sideParentRow = document.getElementById('onto3dSideParentRow');
  var sideParent = document.getElementById('onto3dSideParent');
  var sideName = document.getElementById('onto3dSideName');
  var sideDesc = document.getElementById('onto3dSideDesc');
  var attrsBox = document.getElementById('onto3dSideAttrs');
  var attrsList = document.getElementById('onto3dSideAttrsList');
  var synBox = document.getElementById('onto3dSideSyn');
  var synText = document.getElementById('onto3dSideSynText');
  var sideActions = document.getElementById('onto3dSideActions');
  var sideEditBtn = document.getElementById('onto3dSideEdit');
  var sideDanger = document.getElementById('onto3dSideDanger');
  var sideDeleteBtn = document.getElementById('onto3dSideDeleteBtn');
  var sideLinksBlock = document.getElementById('onto3dSideLinksBlock');
  var fName = document.getElementById('onto3dFName');
  var fDesc = document.getElementById('onto3dFDesc');
  var fParent = document.getElementById('onto3dFParent');
  var fParentLabel = document.getElementById('onto3dFParentLabel');
  var fSyn = document.getElementById('onto3dFSyn');
  var fAttrs = document.getElementById('onto3dFAttrs');
  var fAddAttr = document.getElementById('onto3dFAddAttr');
  var fError = document.getElementById('onto3dFError');
  var fSave = document.getElementById('onto3dFSave');
  var fCancel = document.getElementById('onto3dFCancel');
  var relAddBtn = document.getElementById('onto3dRelAddBtn');
  var relForm = document.getElementById('onto3dSideRelForm');
  var relDir = document.getElementById('onto3dRelDir');
  var relTarget = document.getElementById('onto3dRelTarget');
  var relName = document.getElementById('onto3dRelName');
  var relSubmit = document.getElementById('onto3dRelSubmit');
  var relCancel = document.getElementById('onto3dRelCancel');
  var classesBox = document.getElementById('onto3dClasses');
  var classesGrid = document.getElementById('onto3dClassesGrid');
  var classesSearch = document.getElementById('onto3dClassesSearch');

  /* типы атрибутов — зеркало ATTRIBUTE_TYPES из api/runtime/ontologies.py */
  var ATTR_TYPES = ['string', 'int', 'float', 'bool', 'enum', 'datetime', 'range'];
  var ATTR_TYPE_LABELS = {
    string: 'Текст',
    int: 'Целое число',
    float: 'Дробное число',
    bool: 'Да / нет',
    enum: 'Один из списка',
    datetime: 'Дата и время',
    range: 'Диапазон'
  };

  /* приглушённая палитра в духе дизайн-токенов AIOS */
  var PALETTE = [
    '#1F5AA6', '#2E9D5B', '#6C8B8B', '#8B7C6C', '#7C6C8B',
    '#8B8B6C', '#163F78', '#A08B5D', '#6C7C8B', '#8B6C7C',
    '#6C8B7C', '#8B8B7C'
  ];

  /* ---------- состояние ---------- */

  var onto = null;        /* загруженный граф */
  var nodes = [];         /* {cls, id, x,y,z, vx,vy,vz, deg, color, root} */
  var nodeById = {};
  var links = [];         /* {a, b, rel|null, implicit} */
  var selected = null;
  var hovered = null;
  var lastCard = null;    /* карточка каталога текущей онтологии */
  var editing = { editable: false, token: null, saving: false, open: false };
  var working = { id: null, classes: [], relations: [] };  /* рабочая копия графа */

  var cam = { yaw: 0.6, pitch: 0.35, dist: 620, fov: 900 };
  var alpha = 0;          /* энергия симуляции (0 — замерла) */
  var dragging = false;
  var lastX = 0, lastY = 0;
  var idleAt = 0;
  var dpr = Math.max(1, Math.min(2, window.devicePixelRatio || 1));

  /* ---------- утилиты ---------- */

  function fetchJSON(url) {
    return fetch(url, { headers: { 'Accept': 'application/json' } })
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      });
  }

  function showEmpty(message) {
    emptyEl.hidden = false;
    emptyEl.textContent = message;
  }

  /* ---------- загрузка каталога и графа ---------- */

  function fillSelect(items) {
    var published = items.filter(function (i) { return i && i.file_url; });
    select.innerHTML = '';
    if (!published.length) {
      var opt = document.createElement('option');
      opt.textContent = 'нет опубликованных онтологий';
      opt.disabled = true;
      opt.selected = true;
      select.appendChild(opt);
      showEmpty(
        'Опубликованных онтологий пока нет. Соберите её навыком ' +
        '«Конструктор онтологий» в рабочем чате — после публикации она появится здесь.'
      );
      return;
    }
    published.forEach(function (item) {
      var opt = document.createElement('option');
      opt.value = item.id;
      opt.textContent = item.name || item.id;
      select.appendChild(opt);
    });
    var wanted = new URLSearchParams(window.location.search).get('id');
    if (wanted && published.some(function (i) { return i.id === wanted; })) {
      select.value = wanted;
      loadGraph(select.value);
      return;
    }
    /* запрошенного id нет в каталоге: возможно, это черновик владельца —
       пробуем достать его через API, чтобы не подменять чужой карточкой */
    if (wanted && loadOwned(wanted)) return;
    loadGraph(select.value);
  }

  select.addEventListener('change', function () {
    if (select.value) loadGraph(select.value);
  });

  function renderOwned(card, graph) {
    buildModel(graph);
    renderInfo(card, graph);
    setupEditing(card, graph);
    var bits = [];
    if (card.name) bits.push(card.name);
    bits.push((graph.classes || []).length + ' классов');
    bits.push((graph.relations || []).length + ' связей');
    if (graph.version) bits.push('v' + graph.version);
    metaEl.textContent = bits.join(' · ');
  }

  /* Черновик владельца: GET /api/ontologies/{id} отдаёт его только по токену. */
  function loadOwned(ontologyId) {
    var session = getSession();
    if (!session || !window.AIOS_API || !window.AIOS_API.ontologiesGet) return false;
    metaEl.textContent = 'Загрузка…';
    emptyEl.hidden = true;
    addClassBtn.hidden = true;
    window.AIOS_API.ontologiesGet(session.token, ontologyId)
      .then(function (card) {
        if (card && card.id) renderOwned(card, card);
      })
      .catch(function () {
        showEmpty('Онтология «' + ontologyId + '» не найдена в каталоге ' +
          'и среди ваших черновиков. Опубликуйте её или проверьте ссылку.');
      });
    return true;
  }

  function loadGraph(ontologyId) {
    metaEl.textContent = 'Загрузка…';
    emptyEl.hidden = true;
    addClassBtn.hidden = true;
    fetchJSON('/data/ontologies.json')
      .then(function (data) {
        var items = (data && data.items) || [];
        var card = items.filter(function (i) { return i && i.id === ontologyId; })[0];
        if (!card || !card.file_url) throw new Error('нет файла');
        return fetchJSON(card.file_url).then(function (graph) {
          buildModel(graph);
          renderInfo(card, graph);
          setupEditing(card, graph);
          var bits = [];
          if (card.name) bits.push(card.name);
          bits.push((graph.classes || []).length + ' классов');
          bits.push((graph.relations || []).length + ' связей');
          if (graph.version) bits.push('v' + graph.version);
          metaEl.textContent = bits.join(' · ');
        });
      })
      .catch(function () {
        metaEl.textContent = '';
        infoEl.hidden = true;
        addClassBtn.hidden = true;
        showEmpty('Не удалось загрузить граф онтологии. Проверьте соединение и попробуйте ещё раз.');
      });
  }

  /* ---------- сборка модели графа ---------- */

  function rootAncestorOf(cls, byId) {
    var seen = {};
    var cur = cls;
    while (cur && cur.parent && byId[cur.parent] && !seen[cur.parent]) {
      seen[cur.id] = true;
      cur = byId[cur.parent];
    }
    return cur;
  }

  function buildModel(graph) {
    onto = graph;
    nodes = [];
    nodeById = {};
    links = [];
    selected = null;
    hovered = null;
    hideSide();

    var classes = (graph && graph.classes) || [];
    var relations = (graph && graph.relations) || [];
    var byId = {};
    classes.forEach(function (c) {
      if (c && c.id) byId[String(c.id)] = c;
    });

    /* цвет — по корню иерархии is-a: ветвь дерева = цвет */
    var rootIndex = 0;
    var rootColor = {};
    classes.forEach(function (cls, i) {
      if (!cls || !cls.id) return;
      var root = rootAncestorOf(cls, byId);
      if (!rootColor[root.id]) {
        rootColor[root.id] = PALETTE[rootIndex % PALETTE.length];
        rootIndex += 1;
      }
      var angle = (i / Math.max(1, classes.length)) * Math.PI * 2;
      var r = 120 + 60 * Math.sin(i * 1.7);
      nodes.push({
        cls: cls,
        id: String(cls.id),
        x: r * Math.cos(angle),
        y: Math.sin(i * 2.3) * 80,
        z: r * Math.sin(angle),
        vx: 0, vy: 0, vz: 0,
        deg: 0,
        color: rootColor[root.id],
        root: root
      });
      nodeById[String(cls.id)] = nodes[nodes.length - 1];
    });

    relations.forEach(function (rel) {
      if (!rel) return;
      var a = nodeById[String(rel.domain_class)];
      var b = nodeById[String(rel.range_class)];
      if (!a || !b || a === b) return;
      links.push({ a: a, b: b, rel: rel, implicit: false });
      a.deg += 1;
      b.deg += 1;
    });
    /* иерархия is-a — дополнительные светлые рёбра для раскладки */
    classes.forEach(function (cls) {
      if (!cls || !cls.parent) return;
      var a = nodeById[String(cls.parent)];
      var b = nodeById[String(cls.id)];
      if (!a || !b || a === b) return;
      var exists = links.some(function (l) {
        return l.implicit && l.a === a && l.b === b;
      });
      if (!exists) links.push({ a: a, b: b, rel: null, implicit: true });
    });

    alpha = 1;
    renderLegend();
    renderCq();
    renderClasses(classesSearch ? classesSearch.value : '');
  }

  /* ---------- физика: простая 3D force-directed раскладка ---------- */

  function physicsStep() {
    if (alpha <= 0.004 || nodes.length < 2) return;
    var i, j, n, m, dx, dy, dz, d2, d, f;
    var repulse = 26000 * alpha;
    for (i = 0; i < nodes.length; i++) {
      n = nodes[i];
      for (j = i + 1; j < nodes.length; j++) {
        m = nodes[j];
        dx = n.x - m.x; dy = n.y - m.y; dz = n.z - m.z;
        d2 = dx * dx + dy * dy + dz * dz || 1;
        if (d2 > 90000) continue; /* дальше ~300 — не взаимодействуют */
        f = repulse / d2;
        d = Math.sqrt(d2);
        dx /= d; dy /= d; dz /= d;
        n.vx += dx * f; n.vy += dy * f; n.vz += dz * f;
        m.vx -= dx * f; m.vy -= dy * f; m.vz -= dz * f;
      }
    }
    /* пружины по рёбрам: явные короче, is-a — длиннее и мягче */
    links.forEach(function (l) {
      var rest = l.implicit ? 150 : 110;
      var k = (l.implicit ? 0.012 : 0.02) * alpha;
      dx = l.b.x - l.a.x; dy = l.b.y - l.a.y; dz = l.b.z - l.a.z;
      d = Math.sqrt(dx * dx + dy * dy + dz * dz) || 1;
      f = (d - rest) * k;
      dx /= d; dy /= d; dz /= d;
      l.a.vx += dx * f; l.a.vy += dy * f; l.a.vz += dz * f;
      l.b.vx -= dx * f; l.b.vy -= dy * f; l.b.vz -= dz * f;
    });
    /* притяжение к центру + вязкость */
    nodes.forEach(function (n2) {
      n2.vx -= n2.x * 0.0025 * alpha;
      n2.vy -= n2.y * 0.0025 * alpha;
      n2.vz -= n2.z * 0.0025 * alpha;
      n2.vx *= 0.86; n2.vy *= 0.86; n2.vz *= 0.86;
      n2.x += Math.max(-14, Math.min(14, n2.vx));
      n2.y += Math.max(-14, Math.min(14, n2.vy));
      n2.z += Math.max(-14, Math.min(14, n2.vz));
    });
    alpha *= 0.995;
  }

  /* ---------- проекция и камера ---------- */

  function project(p) {
    var cy = Math.cos(cam.yaw), sy = Math.sin(cam.yaw);
    var cp = Math.cos(cam.pitch), sp = Math.sin(cam.pitch);
    var x = p.x * cy - p.z * sy;
    var z1 = p.x * sy + p.z * cy;
    var y = p.y * cp - z1 * sp;
    var z = p.y * sp + z1 * cp + cam.dist;
    var scale = cam.fov / Math.max(60, z);
    return {
      sx: canvas.width / (2 * dpr) + x * scale,
      sy: canvas.height / (2 * dpr) + y * scale,
      z: z,
      scale: scale
    };
  }

  function nodeRadius(n) {
    return 9 + Math.min(14, n.deg * 2.2);
  }

  /* ---------- отрисовка ---------- */

  var ctx = canvas.getContext('2d');

  function resize() {
    var w = wrap.clientWidth;
    var h = wrap.clientHeight;
    if (h < 40) h = 420;
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(Math.max(320, h) * dpr);
    canvas.style.height = Math.max(320, h) + 'px';
  }
  window.addEventListener('resize', resize);

  function lighten(hex, k) {
    var n = parseInt(hex.slice(1), 16);
    var r = (n >> 16) & 255, g = (n >> 8) & 255, b = n & 255;
    r = Math.round(r + (255 - r) * k);
    g = Math.round(g + (255 - g) * k);
    b = Math.round(b + (255 - b) * k);
    return 'rgb(' + r + ',' + g + ',' + b + ')';
  }

  function render() {
    physicsStep();
    /* лёгкое авто-вращение, когда пользователь не трогает сцену */
    var now = Date.now();
    if (!dragging && now - idleAt > 3000) cam.yaw += 0.0016;

    var w = canvas.width / dpr;
    var h = canvas.height / dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    var proj = {};
    nodes.forEach(function (n) { proj[n.id] = project(n); });

    /* рёбра: дальние сначала (художественный порядок) */
    var drawn = links.map(function (l) {
      var pa = proj[l.a.id], pb = proj[l.b.id];
      return { l: l, pa: pa, pb: pb, z: (pa.z + pb.z) / 2 };
    });
    drawn.sort(function (a, b) { return b.z - a.z; });

    drawn.forEach(function (e) {
      var lit = selected && (e.l.a === selected || e.l.b === selected);
      var depth = Math.max(0.25, Math.min(1, 1 - (e.z - cam.dist) / 900));
      ctx.beginPath();
      ctx.moveTo(e.pa.sx, e.pa.sy);
      ctx.lineTo(e.pb.sx, e.pb.sy);
      if (e.l.implicit) {
        ctx.strokeStyle = 'rgba(106, 112, 96, ' + (0.16 * depth).toFixed(3) + ')';
        ctx.setLineDash([4, 5]);
      } else {
        ctx.strokeStyle = lit
          ? 'rgba(22, 63, 120, ' + (0.85 * depth).toFixed(3) + ')'
          : 'rgba(31, 90, 166, ' + (0.34 * depth).toFixed(3) + ')';
        ctx.setLineDash([]);
      }
      ctx.lineWidth = lit ? 1.8 : 1;
      ctx.stroke();
      ctx.setLineDash([]);
      /* подпись отношения — только у рёбер выбранного класса */
      if (lit && e.l.rel && e.l.rel.name) {
        var mx = (e.pa.sx + e.pb.sx) / 2;
        var my = (e.pa.sy + e.pb.sy) / 2;
        ctx.font = '11px Segoe UI, Arial, sans-serif';
        ctx.fillStyle = 'rgba(22, 63, 120, .95)';
        ctx.textAlign = 'center';
        ctx.fillText(e.l.rel.name, mx, my - 4);
      }
    });

    /* узлы: дальние сначала */
    nodes.slice().sort(function (a, b) {
      return proj[b.id].z - proj[a.id].z;
    }).forEach(function (n) {
      var p = proj[n.id];
      var r = Math.max(3, Math.min(46, nodeRadius(n) * p.scale * (cam.fov / 700)));
      var depth = Math.max(0.3, Math.min(1, 1 - (p.z - cam.dist) / 900));
      var isSel = n === selected;
      var isHov = n === hovered;

      var grad = ctx.createRadialGradient(
        p.sx - r * 0.3, p.sy - r * 0.35, r * 0.15, p.sx, p.sy, r
      );
      grad.addColorStop(0, lighten(n.color, 0.35));
      grad.addColorStop(1, n.color);
      ctx.globalAlpha = 0.45 + 0.55 * depth;
      ctx.beginPath();
      ctx.arc(p.sx, p.sy, r, 0, Math.PI * 2);
      ctx.fillStyle = grad;
      ctx.fill();
      if (isSel || isHov) {
        ctx.lineWidth = 2;
        ctx.strokeStyle = isSel ? '#1B2A41' : 'rgba(27, 42, 65, .55)';
        ctx.stroke();
      }
      ctx.globalAlpha = 1;

      var fs = Math.max(9, Math.min(15, 11 * p.scale * (cam.fov / 700) * 1.4));
      if (fs > 9.4) {
        ctx.font = (isSel ? '600 ' : '') + fs + 'px Segoe UI, Arial, sans-serif';
        ctx.textAlign = 'center';
        ctx.fillStyle = 'rgba(27, 42, 65, ' + (0.5 + 0.5 * depth).toFixed(3) + ')';
        ctx.fillText(String(n.cls.name || n.id), p.sx, p.sy + r + fs * 0.95);
      }
    });

    window.requestAnimationFrame(render);
  }

  /* ---------- взаимодействие: вращение, зум, выбор ---------- */

  function canvasPoint(ev) {
    var rect = canvas.getBoundingClientRect();
    return { x: ev.clientX - rect.left, y: ev.clientY - rect.top };
  }

  function pickNode(pt) {
    var best = null;
    var bestD = 1e9;
    nodes.forEach(function (n) {
      var p = project(n);
      var r = Math.max(3, Math.min(46, nodeRadius(n) * p.scale * (cam.fov / 700)));
      var dx = pt.x - p.sx, dy = pt.y - p.sy;
      var d = Math.sqrt(dx * dx + dy * dy);
      if (d < r + 7 && p.z < bestD) {
        bestD = p.z;
        best = n;
      }
    });
    return best;
  }

  canvas.addEventListener('mousedown', function (e) {
    dragging = true;
    lastX = e.clientX;
    lastY = e.clientY;
    idleAt = Date.now();
  });
  window.addEventListener('mouseup', function () { dragging = false; idleAt = Date.now(); });

  canvas.addEventListener('mousemove', function (e) {
    idleAt = Date.now();
    if (dragging) {
      cam.yaw += (e.clientX - lastX) * 0.006;
      cam.pitch = Math.max(-1.35, Math.min(1.35, cam.pitch + (e.clientY - lastY) * 0.005));
      lastX = e.clientX;
      lastY = e.clientY;
      return;
    }
    hovered = pickNode(canvasPoint(e));
    canvas.style.cursor = hovered ? 'pointer' : 'grab';
  });

  canvas.addEventListener('click', function (e) {
    var n = pickNode(canvasPoint(e));
    if (n) showSide(n);
  });

  canvas.addEventListener('wheel', function (e) {
    e.preventDefault();
    idleAt = Date.now();
    cam.dist = Math.max(180, Math.min(2200, cam.dist * (e.deltaY > 0 ? 1.1 : 0.9)));
  }, { passive: false });

  /* ---------- карточка класса ---------- */

  function showSide(n) {
    selected = n;
    editing.open = false;
    fError.hidden = true;
    relForm.hidden = true;
    document.getElementById('onto3dSideName').textContent = String(n.cls.name || n.id);
    document.getElementById('onto3dSideDesc').textContent = String(n.cls.description || '');
    if (editing.editable) {
      fSave.disabled = false;
      sideParentRow.hidden = !n.cls.parent;
      if (n.cls.parent) {
        var pNode = nodeById[String(n.cls.parent)];
        sideParent.textContent = pNode ? String(pNode.cls.name || pNode.id) : String(n.cls.parent);
      }
    }

    attrsList.innerHTML = '';
    (n.cls.attributes || []).forEach(function (a) {
      if (!a || !a.name) return;
      var li = document.createElement('li');
      var nameSpan = document.createElement('span');
      nameSpan.className = 'onto3d-attr-name';
      nameSpan.textContent = String(a.name);
      li.appendChild(nameSpan);
      if (a.unit) {
        var unitSpan = document.createElement('span');
        unitSpan.className = 'onto3d-attr-unit';
        unitSpan.textContent = String(a.unit);
        li.appendChild(unitSpan);
      }
      attrsList.appendChild(li);
    });
    attrsBox.hidden = !(n.cls.attributes || []).length;

    synText.innerHTML = '';
    (n.cls.synonyms || []).forEach(function (s) {
      if (!s) return;
      var chip = document.createElement('span');
      chip.className = 'onto3d-info-tag';
      chip.textContent = String(s);
      synText.appendChild(chip);
    });

    var linksList = document.getElementById('onto3dSideLinks');
    linksList.innerHTML = '';
    var selfName = String(n.cls.name || n.id);
    var rows = [];
    links.forEach(function (l) {
      if (l.a !== n && l.b !== n) return;
      if (l.implicit) return; /* служебное ребро физики 3D-сцены — не связь для показа */
      var other = l.a === n ? l.b : l.a;
      var label = l.rel && l.rel.name ? l.rel.name : 'связь';
      rows.push({ node: other, otherIsRight: l.a === n, label: label, rel: l.rel });
    });
    if (n.cls.parent && nodeById[String(n.cls.parent)]) {
      var p = nodeById[String(n.cls.parent)];
      rows.unshift({ node: p, otherIsRight: true, label: 'наследует от', rel: null });
    }
    rows.forEach(function (row) {
      var li = document.createElement('li');
      li.className = 'onto3d-rel-row';

      var line = document.createElement('div');
      line.className = 'onto3d-rel-line';

      var selfSpan = document.createElement('span');
      selfSpan.className = 'onto3d-rel-self';
      selfSpan.textContent = selfName;

      var arrowSpan = document.createElement('span');
      arrowSpan.className = 'onto3d-rel-arrow-label';
      arrowSpan.textContent = '→ ' + row.label + ' →';

      var otherBtn = document.createElement('button');
      otherBtn.type = 'button';
      otherBtn.className = 'onto3d-side-link';
      otherBtn.textContent = String(row.node.cls.name || row.node.id);
      otherBtn.addEventListener('click', function () { showSide(row.node); });

      if (row.otherIsRight) {
        line.appendChild(selfSpan);
        line.appendChild(arrowSpan);
        line.appendChild(otherBtn);
      } else {
        line.appendChild(otherBtn);
        line.appendChild(arrowSpan);
        line.appendChild(selfSpan);
      }
      li.appendChild(line);

      if (editing.editable && row.rel) {
        var del = document.createElement('button');
        del.type = 'button';
        del.className = 'onto3d-rel-del';
        del.title = 'Удалить связь';
        del.setAttribute('aria-label', 'Удалить связь');
        del.textContent = '×';
        del.addEventListener('click', function () { deleteRelation(row.rel); });
        li.appendChild(del);
      }
      linksList.appendChild(li);
    });
    if (!rows.length) {
      var li = document.createElement('li');
      li.textContent = 'нет связей';
      linksList.appendChild(li);
    }

    sideActions.hidden = !editing.editable || editing.open;
    sideSaveActions.hidden = !editing.editable || !editing.open;
    sideDanger.hidden = !editing.editable || editing.open;
    relAddBtn.hidden = !editing.editable || editing.open;
    if (editing.editable) fillRelTarget();

    setEditMode(false);
    side.hidden = false;
  }

  /** Переключает поля карточки между просмотром и инлайн-правкой. */
  function setEditMode(on) {
    editing.open = on;
    var view = [sideName, sideDesc, attrsList, synText];
    var edit = [fName, fDesc, fAttrs, fSyn];
    view.forEach(function (el) { if (el) el.hidden = on || elementEmpty(el); });
    edit.forEach(function (el) { el.hidden = !on; });
    fAddAttr.hidden = !on;
    fParent.hidden = !on;
    if (fParentLabel) fParentLabel.hidden = !on;
    sideParentRow.hidden = on || !selected || !selected.cls.parent;
    /* в режиме правки блоки атрибутов и синонимов видны всегда */
    attrsBox.hidden = on ? false : elementEmpty(attrsList);
    synBox.hidden = on ? false : elementEmpty(synText);
    sideActions.hidden = !editing.editable || on;
    sideSaveActions.hidden = !editing.editable || !on;
    sideDanger.hidden = !editing.editable || on;
    relAddBtn.hidden = !editing.editable || on;
    fError.hidden = true;
    if (on) fillParentSelect();
  }

  function elementEmpty(el) {
    return !el || !(el.textContent || '').trim();
  }

  function fillParentSelect() {
    if (!selected) return;
    fParent.innerHTML = '';
    var optNone = document.createElement('option');
    optNone.value = '';
    optNone.textContent = '— без родителя —';
    fParent.appendChild(optNone);
    working.classes.forEach(function (c) {
      if (!c || !c.id || c.id === selected.id) return;
      var o = document.createElement('option');
      o.value = String(c.id);
      o.textContent = String(c.name || c.id);
      fParent.appendChild(o);
    });
    fParent.value = selected.cls.parent ? String(selected.cls.parent) : '';
  }

  /* id класса для повторного выбора после перестройки графа */
  function selectedClassId() {
    return selected ? selected.id : null;
  }

  function hideSide() {
    selected = null;
    side.hidden = true;
  }
  sideClose.addEventListener('click', hideSide);

  /* ============================================================
     Редактирование (владелец онтологии, Bearer-токен).
     Правки применяются к рабочей копии (working) и сохраняются
     PATCH /api/ontologies/{id}: граф (classes+relations) заменяется
     целиком, при статусе active сервер сам перепубликует каталог.
     ============================================================ */

  function getSession() {
    try {
      var s = JSON.parse(localStorage.getItem('aios-auth-v1') || 'null');
      return s && s.token ? s : null;
    } catch (e) { return null; }
  }

  function deepCopy(v) { return JSON.parse(JSON.stringify(v == null ? [] : v)); }

  function uniqueSlug(base, taken) {
    var slug = base;
    while (taken.indexOf(slug) >= 0) slug = base + '-' + Math.random().toString(16).slice(2, 6);
    return slug;
  }

  /** Проверяет, что токен принадлежит владельцу онтологии, и включает правку. */
  function setupEditing(card, graph) {
    lastCard = card;
    working.id = card.id;
    working.classes = deepCopy(graph.classes);
    working.relations = deepCopy(graph.relations);
    editing.editable = false;
    editing.token = null;
    addClassBtn.hidden = true;
    var session = getSession();
    if (!session || !window.AIOS_API || !window.AIOS_API.ontologiesGet) return;
    editing.token = session.token;
    var enable = function () {
      editing.editable = true;
      addClassBtn.hidden = false;
      if (!side.hidden && selected) showSide(selected);
    };
    window.AIOS_API.ontologiesGet(session.token, card.id)
      .then(enable)
      .catch(function (err) {
        /* владелец получает 200; администратору онтология не принадлежит
           (404), но правку ему разрешает бэкенд */
        if (session.role === 'Администратор' && err && err.status === 404) enable();
      });
  }

  /** Сохраняет рабочую копию на сервере и перестраивает сцену из ответа. */
  function persistChanges(reselectId, thenOpenEditor) {
    if (!editing.editable || editing.saving) return Promise.resolve();
    editing.saving = true;
    fSave.disabled = true;
    fError.hidden = true;
    return window.AIOS_API.ontologiesUpdate(editing.token, working.id, {
      classes: working.classes,
      relations: working.relations
    }).then(function (saved) {
      working.classes = deepCopy(saved.classes);
      working.relations = deepCopy(saved.relations);
      buildModel(saved);
      if (lastCard) renderInfo(lastCard, saved);
      var bits = [];
      if (lastCard && lastCard.name) bits.push(lastCard.name);
      bits.push((saved.classes || []).length + ' классов');
      bits.push((saved.relations || []).length + ' связей');
      if (saved.version) bits.push('v' + saved.version);
      metaEl.textContent = bits.join(' · ');
      var node = reselectId && nodeById[String(reselectId)];
      if (node) {
        showSide(node);
        if (thenOpenEditor) openEditor(node);
      }
    }).catch(function (err) {
      var msg = err && err.message ? err.message : 'Не удалось сохранить изменения';
      if (!side.hidden) {
        fError.textContent = msg;
        fError.hidden = false;
      } else {
        window.alert(msg);
      }
    }).then(function () {
      editing.saving = false;
      fSave.disabled = false;
    });
  }

  /* ---------- форма класса ---------- */

  function openEditor(node) {
    var cls = node.cls;
    var orig = working.classes.filter(function (c) { return c && c.id === cls.id; })[0] || cls;

    fName.value = String(cls.name || '');
    fDesc.value = String(cls.description || '');
    fSyn.value = (cls.synonyms || []).join(', ');

    fAttrs.innerHTML = '';
    var attrs = (orig.attributes || []);
    if (!attrs.length) addAttrRow(null);
    attrs.forEach(function (a) { addAttrRow(a); });

    setEditMode(true);
  }

  function addAttrRow(a) {
    a = a || {};
    var row = document.createElement('div');
    row.className = 'onto3d-attr-row';
    row.dataset.attrId = a.id ? String(a.id) : '';

    var name = document.createElement('input');
    name.className = 'onto3d-input';
    name.placeholder = 'Название';
    name.maxLength = 120;
    name.value = a.name ? String(a.name) : '';

    var type = document.createElement('select');
    type.className = 'onto3d-input';
    var t = a.type ? String(a.type) : 'string';
    if (ATTR_TYPES.indexOf(t) < 0) ATTR_TYPES = ATTR_TYPES.concat(t);
    ATTR_TYPES.forEach(function (tp) {
      var o = document.createElement('option');
      o.value = tp;
      o.textContent = ATTR_TYPE_LABELS[tp] || tp;
      type.appendChild(o);
    });
    type.value = t;

    var unit = document.createElement('input');
    unit.className = 'onto3d-input';
    unit.placeholder = 'Ед. изм.';
    unit.maxLength = 20;
    unit.value = a.unit ? String(a.unit) : '';

    var del = document.createElement('button');
    del.type = 'button';
    del.className = 'onto3d-attr-del';
    del.title = 'Удалить атрибут';
    del.setAttribute('aria-label', 'Удалить атрибут');
    del.textContent = '×';
    del.addEventListener('click', function () {
      row.parentNode.removeChild(row);
      if (!fAttrs.children.length) addAttrRow(null);
    });

    row.appendChild(name); row.appendChild(type); row.appendChild(unit); row.appendChild(del);
    fAttrs.appendChild(row);
  }

  function collectClass() {
    var cls = deepCopy(working.classes.filter(function (c) { return c && String(c.id) === selected.id; })[0] || selected.cls);
    cls.id = selected.id;
    cls.name = fName.value.trim();
    cls.description = fDesc.value.trim();
    cls.parent = fParent.value || '';
    if (!cls.parent) delete cls.parent;
    cls.synonyms = fSyn.value.split(',').map(function (s) { return s.trim(); }).filter(Boolean);

    var taken = working.classes.map(function (c) { return c && c.id; });
    var attrs = [];
    Array.prototype.forEach.call(fAttrs.children, function (row) {
      var name = row.children[0].value.trim();
      if (!name) return;
      var aid = row.dataset.attrId;
      if (!aid || !/^[a-z0-9][a-z0-9_-]{1,63}$/.test(aid)) aid = uniqueSlug('attr', taken);
      taken.push(aid);
      var attr = { id: aid, name: name, type: row.children[1].value };
      var unit = row.children[2].value.trim();
      if (unit) attr.unit = unit;
      attrs.push(attr);
    });
    cls.attributes = attrs;
    return cls;
  }

  function saveClass() {
    var cls = collectClass();
    if (!cls.name) {
      fError.textContent = 'Заполните название класса';
      fError.hidden = false;
      return;
    }
    var replaced = false;
    working.classes = working.classes.map(function (c) {
      if (c && c.id === cls.id) { replaced = true; return cls; }
      return c;
    });
    if (!replaced) working.classes.push(cls);
    var cid = cls.id;
    persistChanges(cid).then(function () { /* форма закрыта при успехе через showSide */ });
  }

  function deleteSelectedClass() {
    if (!selected) return;
    var cid = selected.id;
    var nm = String(selected.cls.name || cid);
    if (!window.confirm('Удалить класс «' + nm + '» и все его связи?')) return;
    working.classes = working.classes.filter(function (c) { return !c || c.id !== cid; });
    working.relations = working.relations.filter(function (r) {
      return r && r.domain_class !== cid && r.range_class !== cid;
    });
    persistChanges().then(function () { hideSide(); });
  }

  /* ---------- связи ---------- */

  function fillRelTarget() {
    relTarget.innerHTML = '';
    working.classes.forEach(function (c) {
      if (!c || !c.id) return;
      var o = document.createElement('option');
      o.value = String(c.id);
      o.textContent = String(c.name || c.id);
      relTarget.appendChild(o);
    });
  }

  function deleteRelation(rel) {
    if (!rel || !rel.id) return;
    if (!window.confirm('Удалить связь «' + (rel.name || rel.id) + '»?')) return;
    working.relations = working.relations.filter(function (r) { return !r || r.id !== rel.id; });
    persistChanges(selectedClassId());
  }

  function addRelation() {
    var target = relTarget.value;
    var nm = relName.value.trim();
    if (!target || !nm) {
      window.alert('Укажите название связи и целевой класс');
      return;
    }
    var taken = working.relations.map(function (r) { return r && r.id; });
    var rid = uniqueSlug('rel', taken);
    var rel = {
      id: rid,
      name: nm,
      domain_class: relDir.value === 'in' ? String(target) : selected.id,
      range_class: relDir.value === 'in' ? selected.id : String(target)
    };
    working.relations.push(rel);
    relName.value = '';
    relForm.hidden = true;
    persistChanges(selectedClassId());
  }

  /* ---------- новый класс ---------- */

  function addClass() {
    var taken = working.classes.map(function (c) { return c && c.id; });
    var cid = uniqueSlug('cls', taken);
    working.classes.push({ id: cid, name: 'Новый класс', attributes: [] });
    persistChanges(cid, true);
  }

  /* ---------- события ---------- */

  sideEditBtn.addEventListener('click', function () { if (selected) openEditor(selected); });
  fCancel.addEventListener('click', function () { if (selected) showSide(selected); });
  fSave.addEventListener('click', function () { saveClass(); });
  fAddAttr.addEventListener('click', function () { addAttrRow(null); });
  sideDeleteBtn.addEventListener('click', deleteSelectedClass);
  addClassBtn.addEventListener('click', addClass);
  relAddBtn.addEventListener('click', function () { relForm.hidden = !relForm.hidden; });
  relCancel.addEventListener('click', function () { relForm.hidden = true; });
  relSubmit.addEventListener('click', addRelation);


  /* ---------- карточка онтологии ---------- */

  function addFact(label, value) {
    if (!value) return;
    var dt = document.createElement('dt');
    var dd = document.createElement('dd');
    dt.textContent = label;
    dd.textContent = String(value);
    infoFacts.appendChild(dt);
    infoFacts.appendChild(dd);
  }

  function renderInfo(card, graph) {
    infoName.textContent = String(card.name || graph.name || card.id);
    var desc = graph.description || card.description_ru || card.domain || '';
    infoDesc.textContent = String(desc);
    infoDesc.hidden = !desc;

    infoTags.innerHTML = '';
    var tags = card.tags || graph.tags || [];
    tags.forEach(function (t) {
      if (!t) return;
      var chip = document.createElement('span');
      chip.className = 'onto3d-info-tag';
      chip.textContent = String(t);
      infoTags.appendChild(chip);
    });
    infoTags.hidden = !infoTags.children.length;

    infoFacts.innerHTML = '';
    addFact('Предметная область', card.domain || graph.domain);
    addFact('Автор', card.author || graph.author);
    addFact('Статус', card.status || graph.status);
    addFact('Версия', graph.version ? 'v' + graph.version : null);
    addFact('Классы', (graph.classes || []).length || card.classes_count);
    addFact('Связи', (graph.relations || []).length || card.relations_count);
    addFact('Компетентностные вопросы', ((graph.competency_questions || []).length) || null);
    infoEl.hidden = false;
  }

  /* ---------- легенда и компетентностные вопросы ---------- */

  function renderLegend() {
    legendEl.innerHTML = '';
    var seen = {};
    var chips = [];
    nodes.forEach(function (n) {
      var key = n.root.id;
      if (seen[key]) return;
      seen[key] = true;
      chips.push({ color: n.color, name: String(n.root.name || n.root.id) });
    });
    if (chips.length < 2) return;
    chips.slice(0, 10).forEach(function (c) {
      var chip = document.createElement('span');
      chip.className = 'onto3d-legend-chip';
      var dot = document.createElement('i');
      dot.style.background = c.color;
      chip.appendChild(dot);
      chip.appendChild(document.createTextNode(c.name));
      legendEl.appendChild(chip);
    });
    legendEl.hidden = false;
  }

  function idsMentioned(text) {
    var re = /\b(?:cls|rel)-[a-z0-9_-]+/gi;
    return String(text || '').match(re) || [];
  }

  function humanizePath(path, classNameById, relNameById) {
    return String(path || '').replace(/\b(cls|rel)-[a-z0-9_-]+\b/gi, function (token) {
      if (/^cls-/i.test(token)) return classNameById[token] || token;
      if (/^rel-/i.test(token)) return relNameById[token] || token;
      return token;
    });
  }

  function renderCq() {
    cqList.innerHTML = '';
    var cqs = (onto && onto.competency_questions) || [];
    cqBox.hidden = !cqs.length;
    if (!cqs.length) { if (cqSummary) cqSummary.textContent = ''; return; }

    var knownIds = {};
    var classNameById = {};
    var relNameById = {};
    ((onto && onto.classes) || []).forEach(function (c) {
      if (!c || !c.id) return;
      knownIds[String(c.id)] = true;
      classNameById[String(c.id)] = c.name || c.id;
    });
    ((onto && onto.relations) || []).forEach(function (r) {
      if (!r || !r.id) return;
      knownIds[String(r.id)] = true;
      relNameById[String(r.id)] = r.name || r.id;
    });

    var covered = 0, broken = 0, none = 0;

    cqs.forEach(function (cq) {
      if (!cq || !cq.question) return;
      var paths = (cq.coverage || []).filter(Boolean);
      var status;
      if (!paths.length) {
        status = 'none';
      } else {
        var allOk = paths.every(function (path) {
          var ids = idsMentioned(path);
          if (!ids.length) return true;
          return ids.every(function (id) { return knownIds[id]; });
        });
        status = allOk ? 'ok' : 'broken';
      }
      if (status === 'ok') covered += 1;
      else if (status === 'broken') broken += 1;
      else none += 1;

      var li = document.createElement('li');
      li.className = 'onto3d-cq-item onto3d-cq-item--' + status;

      var head = document.createElement('div');
      head.className = 'onto3d-cq-head';
      var q = document.createElement('p');
      q.className = 'onto3d-cq-q';
      q.textContent = String(cq.question);
      var badge = document.createElement('span');
      badge.className = 'onto3d-cq-status onto3d-cq-status--' + status;
      badge.textContent = status === 'ok' ? 'обеспечен' : (status === 'broken' ? 'нарушен правкой' : 'нет покрытия');
      head.appendChild(q);
      head.appendChild(badge);
      li.appendChild(head);

      paths.forEach(function (path) {
        var p = document.createElement('code');
        p.className = 'onto3d-cq-path';
        var ids = idsMentioned(path);
        var brokenPath = ids.length > 0 && !ids.every(function (id) { return knownIds[id]; });
        if (brokenPath) p.className += ' onto3d-cq-path--broken';
        p.textContent = humanizePath(path, classNameById, relNameById);
        li.appendChild(p);
      });
      cqList.appendChild(li);
    });

    if (cqSummary) {
      var total = cqs.length;
      if (broken > 0) {
        cqSummary.textContent = 'Обеспечено графом: ' + covered + ' из ' + total +
          '. Правка нарушила покрытие у ' + broken + ' — проверьте связи, отмеченные ниже.';
        cqSummary.className = 'onto3d-cq-summary onto3d-cq-summary--warn';
      } else {
        cqSummary.textContent = 'Обеспечено графом: ' + covered + ' из ' + total + '.';
        cqSummary.className = 'onto3d-cq-summary';
      }
    }
  }

  /* ---------- сетка карточек классов под 3D-сценой ---------- */

  function renderClasses(filter) {
    if (!classesGrid) return;
    var q = String(filter || '').trim().toLowerCase();
    classesGrid.innerHTML = '';
    var list = nodes.filter(function (n) {
      if (!q) return true;
      var hay = [n.cls.name, (n.cls.synonyms || []).join(' ')].join(' ').toLowerCase();
      return hay.indexOf(q) >= 0;
    });
    list.forEach(function (n) {
      var orphan = !n.cls.parent && n.deg === 0;
      var card = document.createElement('button');
      card.type = 'button';
      card.className = 'onto3d-class-card' + (orphan ? ' onto3d-class-card--orphan' : '');

      var dot = document.createElement('i');
      dot.className = 'onto3d-class-card-dot';
      dot.style.background = n.color;
      card.appendChild(dot);

      var body = document.createElement('div');
      body.className = 'onto3d-class-card-body';

      var name = document.createElement('div');
      name.className = 'onto3d-class-card-name';
      name.textContent = String(n.cls.name || n.id);
      body.appendChild(name);

      if (n.cls.description) {
        var desc = document.createElement('p');
        desc.className = 'onto3d-class-card-desc';
        desc.textContent = String(n.cls.description);
        body.appendChild(desc);
      }

      var meta = document.createElement('div');
      meta.className = 'onto3d-class-card-meta';
      var bits = [(n.cls.attributes || []).length + ' атр.', n.deg + ' связ.'];
      if ((n.cls.synonyms || []).length) bits.push((n.cls.synonyms || []).length + ' син.');
      meta.textContent = bits.join(' \u00b7 ');
      body.appendChild(meta);

      if (orphan) {
        var badge = document.createElement('span');
        badge.className = 'onto3d-class-card-badge';
        badge.textContent = '\u043d\u0435\u0442 \u0441\u0432\u044f\u0437\u0435\u0439';
        body.appendChild(badge);
      }

      card.appendChild(body);
      card.addEventListener('click', function () {
        showSide(n);
        wrap.scrollIntoView({ behavior: 'smooth', block: 'start' });
      });
      classesGrid.appendChild(card);
    });
    if (classesBox) classesBox.hidden = !nodes.length;
  }

  if (classesSearch) {
    classesSearch.addEventListener('input', function () { renderClasses(classesSearch.value); });
  }

  /* ---------- всплывающие подсказки-глоссарий: JS-позиционирование ----------
     CSS-вариант через ::after обрезался внутри .onto3d-side (overflow:auto),
     поэтому подсказка — отдельный элемент в document.body, position:fixed,
     координаты считаются от кнопки и подрезаются под края экрана. */
  var tipEl = document.createElement('div');
  tipEl.className = 'onto3d-tooltip';
  document.body.appendChild(tipEl);

  function showTip(btn) {
    var text = btn.getAttribute('data-tip');
    if (!text) return;
    tipEl.textContent = text;
    tipEl.style.left = '-9999px';
    tipEl.style.top = '-9999px';
    tipEl.classList.add('is-visible');
    var r = btn.getBoundingClientRect();
    var tr = tipEl.getBoundingClientRect();
    var left = r.left + r.width / 2 - tr.width / 2;
    left = Math.max(8, Math.min(left, window.innerWidth - tr.width - 8));
    var top = r.top - tr.height - 8;
    if (top < 8) top = r.bottom + 8;
    tipEl.style.left = left + 'px';
    tipEl.style.top = top + 'px';
  }
  function hideTip() { tipEl.classList.remove('is-visible'); }

  document.addEventListener('mouseover', function (e) {
    var btn = e.target.closest && e.target.closest('.onto3d-help-btn');
    if (btn) showTip(btn);
  });
  document.addEventListener('mouseout', function (e) {
    var btn = e.target.closest && e.target.closest('.onto3d-help-btn');
    if (btn) hideTip();
  });
  document.addEventListener('focusin', function (e) {
    if (e.target && e.target.classList && e.target.classList.contains('onto3d-help-btn')) showTip(e.target);
  });
  document.addEventListener('focusout', function (e) {
    if (e.target && e.target.classList && e.target.classList.contains('onto3d-help-btn')) hideTip();
  });

  /* ---------- запуск ---------- */

  resize();
  canvas.style.cursor = 'grab';
  window.requestAnimationFrame(render);
  fetchJSON('/data/ontologies.json')
    .then(function (data) { fillSelect((data && data.items) || []); })
    .catch(function () {
      showEmpty('Не удалось загрузить каталог онтологий. Проверьте соединение и попробуйте ещё раз.');
    });
})();

