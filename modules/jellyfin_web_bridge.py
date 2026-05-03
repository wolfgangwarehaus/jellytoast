"""Jellyfin Web bridge — embedded-page glue for the QtWebEngine view.

Three things live here:

- ``SHIM_JS`` — the JS shim injected into every Jellyfin Web page. Hides
  the JF Web bottom transport bar, blocks JF Web's "Playback failed"
  toasts, exposes helpers for tab/drawer navigation, sniffs queue
  state, and pushes the user's ApiClient credentials over the
  ``QWebChannel`` so Python can call ``/Users/{user_id}/...`` endpoints
  without re-authenticating.

- ``Bridge`` — the ``QObject`` exposed as ``window.jellytoast`` via
  ``QWebChannel``. JS calls its ``@Slot`` methods (``shuffleClicked``,
  ``pageReady``, ``pageRendered``, ``setCredentials``); Python listens
  to its signals (``shuffle_requested``, ``page_ready``,
  ``page_rendered``, ``credentials_received``) to drive the host.

- ``_LoggingPage`` — a ``QWebEnginePage`` subclass that routes JF Web's
  JS console messages to the terminal so we can post-mortem from logs
  alone.

These were previously inlined in ``jellytoast.py`` (~750 lines of JS
plus a small QObject); pulling them out drops ~30% of the main entry
file's size and isolates the JF-Web-coupled surface for future
refactoring.
"""

import re
import time

from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtWebEngineCore import QWebEnginePage


SHIM_JS = r"""
(function() {
  if (window.__jellytoast_installed) return;
  window.__jellytoast_installed = true;
  console.log('[JellyToast] shim loaded at', location.href);

  // Hide the page until we've finished navigating to the music library.
  // _on_first_load (Python) calls window.__jellytoast_reveal() once it's done.
  // The reveal pings the host TWICE via QWebChannel:
  //   pageReady    — DOM is fully laid out; host can show the window.
  //                  Wayland uses this signal (since it can't show
  //                  invisibly while waiting for art to load).
  //   pageRendered — page has actually composited to screen (we know
  //                  because document.visibilityState went 'visible'
  //                  AND a few rAF callbacks fired at full speed);
  //                  host can hide the loading overlay. X11 uses this
  //                  to flip opacity 0 → 1 in one go.
  // Splitting this lets the X11 overlay stay up through Chromium's
  // post-show compositor cycle so chrome and albums appear together.
  document.documentElement.classList.add('jt-loading');
  function pingHost(method) {
    try {
      if (window.jellytoast && typeof window.jellytoast[method] === 'function') {
        window.jellytoast[method]();
      }
    } catch (e) { /* bridge not ready; failsafe timers will catch up */ }
  }
  window.__jellytoast_reveal = function() {
    document.documentElement.classList.remove('jt-loading');
    pingHost('pageReady');
    // After the host shows the window (in response to pageReady),
    // the page becomes 'visible' and JF Web's lazy-loader starts
    // pulling cover art. We don't ping pageRendered until those
    // images have actually arrived AND the page has had a frame to
    // composite them — otherwise the host hides the overlay while
    // album cards still have empty placeholders, and the art "pops
    // in" after.
    function waitForVisibleAndArt() {
      if (document.visibilityState !== 'visible') {
        setTimeout(waitForVisibleAndArt, 50);
        return;
      }
      // Wait for the batch of visible cover-art images to finish
      // loading. JF Web's lazy loader fires them in waves once the
      // page is visible; we look for the count of fully-loaded
      // images to STOP increasing for 600ms (6 ticks at 100ms),
      // which signals that the current viewport's images are all in.
      // Without this, pageRendered fires after the first batch and
      // the user sees the rest of the grid pop in afterwards.
      // Filter naturalWidth > 100 to exclude blurhash placeholders
      // (typically ~20×20) — counting them would ping pageRendered
      // before the real images swap in.
      var deadline = Date.now() + 8000;
      var prev = 0;
      var stableTicks = 0;
      var iv = setInterval(function() {
        var images = document.querySelectorAll(
          '.itemsContainer img, .libraryPage img, '
          + '.pageTabContent img, .homePage img'
        );
        var loaded = 0;
        for (var i = 0; i < images.length; i++) {
          if (images[i].complete && images[i].naturalWidth > 100) loaded++;
        }
        if (loaded >= 4 && loaded === prev) {
          stableTicks++;
        } else {
          stableTicks = 0;
          prev = loaded;
        }
        if (stableTicks >= 6 || Date.now() > deadline) {
          clearInterval(iv);
          // A handful of rAFs so the loaded images have actually
          // composited to the screen surface before we yank the
          // overlay.
          var frames = 0;
          (function loop() {
            if (++frames >= 4) { pingHost('pageRendered'); return; }
            requestAnimationFrame(loop);
          })();
        }
      }, 100);
    }
    waitForVisibleAndArt();
  };
  // Failsafe: never stay hidden for more than 8s, even if navigation hangs.
  setTimeout(window.__jellytoast_reveal, 8000);

  function bind() {
    if (typeof QWebChannel === 'undefined' || !window.qt || !qt.webChannelTransport) {
      return setTimeout(bind, 50);
    }
    new QWebChannel(qt.webChannelTransport, function(channel) {
      window.jellytoast = channel.objects.bridge;
      console.log('[JellyToast] bridge ready');
      window.__jellytoast_push_credentials();
    });
  }

  // Push JF Web's current sign-in to Python whenever it appears or
  // changes. The host caches user_id + token so its REST client can
  // talk to /Users/{user_id}/... endpoints (intent fallbacks, library
  // resolution, etc.).
  //
  // We read the canonical `jellyfin_credentials` blob from localStorage
  // — that's where JF Web persists the active server's accessor data
  // and is stable across versions. ApiClient method getters
  // (accessToken(), serverAddress()) come and go between releases;
  // localStorage is reliable.
  window.__jellytoast_push_credentials = function() {
    var lastKey = '';
    function pickActive(blob) {
      try {
        var data = JSON.parse(blob);
        var servers = (data && data.Servers) || [];
        if (!servers.length) return null;
        // Prefer the most recently accessed server.
        servers.sort(function(a, b) {
          return (b.DateLastAccessed || 0) - (a.DateLastAccessed || 0);
        });
        var s = servers[0];
        if (!s.UserId || !s.AccessToken) return null;
        var srv = s.ManualAddress || s.LocalAddress || s.RemoteAddress || '';
        if (!srv) return null;
        return { srv: srv, uid: s.UserId, tok: s.AccessToken };
      } catch (e) { return null; }
    }
    function tick() {
      try {
        var blob = localStorage.getItem('jellyfin_credentials');
        if (!blob) return;
        var c = pickActive(blob);
        if (!c) return;
        var key = c.srv + '|' + c.uid + '|' + c.tok;
        if (key === lastKey) return;
        lastKey = key;
        if (window.jellytoast && window.jellytoast.setCredentials) {
          window.jellytoast.setCredentials(c.srv, c.uid, c.tok);
          console.log('[JellyToast] pushed credentials from localStorage');
        }
      } catch (e) { /* ignore */ }
    }
    setInterval(tick, 1500);
    tick();
  };

  function injectCSS() {
    if (document.getElementById('jellytoast-css')) return;
    const style = document.createElement('style');
    style.id = 'jellytoast-css';
    style.textContent = `
      /* Make Jellyfin Web's app shell transparent so the host window's
         frosted body shows through. Library cards keep their own fills.
         (.mainDrawer is intentionally NOT in this list — see below; it
         gets a frosted background of its own.) */
      html, body,
      .skinBody, .skinBody-withBackdrop,
      .mainAnimatedPages, .mainAnimatedPage,
      .page, .libraryPage, .itemDetailPage, .homePage, .homePage-content,
      .pageContainer, .dialogBackdrop,
      .backdropContainer, .backgroundContainer,
      .mainAnimatedPagesContainer { background: transparent !important; }
      /* Hide Jellyfin Web's .skinHeader entirely — JellyToast paints its
         own native top bar (modules/top_bar.py) above the WebView. Then
         reclaim the empty top strip the .skinHeader used to occupy by
         pulling .skinBody up to top: 0. Without this, content would
         start ~7em down the WebView with empty space above it. */
      .skinHeader,
      .skinHeader-withBackground,
      .skinHeader.semiTransparent { display: none !important; }
      .skinBody,
      .skinBody-withBackdrop { top: 0 !important; }
      /* Pages bake spacing in for the now-hidden .skinHeader. Hit
         padding-top, margin-top, AND top: in case it's an absolute. */
      .page,
      .libraryPage,
      .itemDetailPage,
      .homePage,
      .padded-top-page,
      .padded-top,
      .pageContainer,
      .mainAnimatedPagesContainer,
      .mainAnimatedPages,
      .libraryHeader,
      .headerSpacer,
      .headerSection,
      .padded-top-section,
      .padded-top-headroom,
      .pageWithAbsoluteTabs,
      .withTabs {
        padding-top: 0 !important;
        margin-top: 0 !important;
        top: 0 !important;
      }
      .libraryPage > .pageTabContent,
      .absolutePageTabContent,
      .libraryPage .padded-top { top: 0 !important; }
      .mainDrawer,
      .mainDrawer-scrollContainer {
        background: rgba(24, 24, 24, 0.72) !important;
        box-shadow: none !important;
        border-right: none !important;
      }
      /* Hide all scrollbars — Jellyfin Web has its own letter scrubber
         and infinite-scroll cards, the chrome scrollbar is just noise. */
      ::-webkit-scrollbar { width: 0 !important; height: 0 !important;
        background: transparent !important; }
      ::-webkit-scrollbar-thumb,
      ::-webkit-scrollbar-track,
      ::-webkit-scrollbar-corner { background: transparent !important; }
      * { scrollbar-width: none !important; }
      /* Force the system default cursor everywhere inside the embedded
         view. Jellyfin Web sets cursor:pointer on every card, link, and
         button — that creates a visible pointer-finger hop every time
         the cursor crosses an album. We override to default so the
         cursor stays the system arrow over the whole web view. */
      *, *::before, *::after { cursor: default !important; }
      input, textarea, [contenteditable='true'] { cursor: text !important; }
      html.jt-loading body { opacity: 0 !important; }
      html:not(.jt-loading) body { transition: opacity 320ms ease-out; }
      .nowPlayingBar,
      .nowPlayingBarTop,
      .nowPlayingBarBottom { display: none !important; }
      .mainAnimatedPages { padding-bottom: 0 !important; }
      /* Cast button in the header — non-functional in JellyToast (we have
         our own cast manager in the now-playing bar). */
      .headerCastButton,
      .btnCast,
      button[is="paper-icon-button-light"].headerCastButton { display: none !important; }
      /* Loading spinners — JF Web shows .docspinner while fetching its
         own playback queue (300 random items, prefetch metadata, etc.)
         and trying to set up its audio element. We've already taken
         over via mpv, the queue is already in our cache, so this
         spinner is just visual noise that lingers for ~3s before its
         audio.src finally errors out. Hide it; legitimate page-load
         spinners on the main view aren't using this class. */
      .docspinner,
      .mdl-spinner,
      .spinnerContainer { display: none !important; }
      /* Hide dialogs/toasts until our observer has approved them.
         This kills the "Playback failed" flash before it can paint. */
      dialog:not(.jt-checked),
      .dialog:not(.jt-checked),
      .dialogContainer:not(.jt-checked),
      .paperDialog:not(.jt-checked),
      .formDialog:not(.jt-checked),
      .actionSheet:not(.jt-checked),
      .actionsheet:not(.jt-checked),
      .toast:not(.jt-checked),
      .toast-message:not(.jt-checked),
      [role="alertdialog"]:not(.jt-checked),
      [role="dialog"]:not(.jt-checked) { visibility: hidden !important; }
    `;
    (document.head || document.documentElement).appendChild(style);
  }

  // Suppress Jellyfin Web's "Playback failed / not supported by this client"
  // alert that fires after our URL interceptor blocks the audio request.
  // Acts only when phrase is inside a known dialog container — never walks
  // up into the main app shell.
  const KILL_PHRASES = [
    'not supported by this client',
    'no compatible streams',
    'playback error',
  ];
  const TOAST_SELECTOR = [
    'dialog', '.dialog', '.dialogContainer', '.paperDialog', '.formDialog',
    '.actionsheet', '.actionSheet', '.toast', '.toast-message',
    '[role="alertdialog"]', '[role="dialog"]',
  ].join(', ');

  // Wrap window.alert in case Jellyfin Web ever uses it directly.
  const _origAlert = window.alert;
  window.alert = function(msg) {
    if (typeof msg === 'string' &&
        KILL_PHRASES.some(p => msg.toLowerCase().includes(p))) {
      console.log('[JellyToast] suppressed alert:', msg);
      return;
    }
    return _origAlert.apply(this, arguments);
  };

  function isKillText(text) {
    text = (text || '').toLowerCase();
    return KILL_PHRASES.some(p => text.includes(p));
  }
  function findToastAncestor(node) {
    let cur = node;
    for (let i = 0; cur && i < 12; i++) {
      if (cur.matches && cur.matches(TOAST_SELECTOR)) return cur;
      cur = cur.parentElement;
    }
    return null;
  }
  // Decide each new dialog: kill it (if it carries a kill phrase) or mark
  // it jt-checked so the CSS reveals it. We check on requestAnimationFrame,
  // which runs *before* the next paint — so a killed dialog never paints.
  // Some dialogs are inserted empty and populated a tick later; we re-check
  // for up to 30 frames before giving up and revealing.
  function decide(toast) {
    let attempts = 0;
    function check() {
      if (!toast.isConnected) return;
      attempts++;
      const text = (toast.textContent || '').trim();
      if (isKillText(text)) {
        try {
          const btn = toast.querySelector(
            'button[data-id="ok"], .button-submit, .btnCloseDialog, .raised-cancel, .dialog-button'
          );
          if (btn) btn.click(); else toast.remove();
        } catch (e) { /* ignore */ }
        console.log('[JellyToast] suppressed playback dialog');
        return;
      }
      if (text || attempts > 30) {
        toast.classList.add('jt-checked');
        return;
      }
      requestAnimationFrame(check);
    }
    requestAnimationFrame(check);
  }
  function handleNode(n) {
    if (!n || n.nodeType !== 1) return;
    if (n.matches && n.matches(TOAST_SELECTOR)) { decide(n); return; }
    if (n.querySelectorAll) {
      n.querySelectorAll(TOAST_SELECTOR).forEach(decide);
    }
    // Lazy-populated case: text appears inside a pre-existing dialog.
    if (isKillText(n.textContent)) {
      const toast = findToastAncestor(n);
      if (toast) decide(toast);
    }
  }
  function watchDialogs() {
    // Pre-existing dialogs (shouldn't be any, but defensive).
    document.querySelectorAll(TOAST_SELECTOR).forEach(decide);
    const obs = new MutationObserver(muts => {
      for (const m of muts) {
        for (const n of m.addedNodes) handleNode(n);
      }
    });
    obs.observe(document.body, { childList: true, subtree: true });
  }

  // Runtime gap-killer: Jellyfin Web's pages bake spacing into
  // .libraryPage / .itemDetailPage / .homePage / .padded-top to clear
  // the (now-hidden) .skinHeader. Static CSS misses some classes /
  // inline styles; this re-applies on every page change. Hits all
  // three vectors — padding, margin, and absolute top.
  function killTopPadding() {
    const sels = [
      '.libraryPage', '.itemDetailPage', '.homePage', '.page',
      '.padded-top-page', '.padded-top', '.padded-top-section',
      '.padded-top-headroom', '.libraryHeader', '.headerSpacer',
      '.mainAnimatedPagesContainer', '.mainAnimatedPages',
      '.pageWithAbsoluteTabs', '.withTabs',
    ];
    for (const s of sels) {
      document.querySelectorAll(s).forEach(el => {
        // setProperty with 'important' is the only way to beat
        // Jellyfin Web's `.pageWithAbsoluteTabs { padding-top: 7em
        // !important }`. Plain inline el.style.paddingTop loses to any
        // external !important rule.
        el.style.setProperty('padding-top', '0', 'important');
        el.style.setProperty('margin-top', '0', 'important');
      });
    }
  }
  window.addEventListener('hashchange', () => setTimeout(killTopPadding, 50));
  setInterval(killTopPadding, 750);

  // Library tab switcher — finds Jellyfin Web's tab button by its
  // visible label and clicks it. Used by JtTopBar's "View" dropdown
  // to drive Albums/Songs/Genres/etc. without our own routing logic.
  // Falls back to URL-hash manipulation (?tab=N) if no button matches,
  // since Jellyfin Web's library pages parse the tab index from there.
  const _TAB_BUTTON_SELECTORS = [
    'button.emby-tabs-button',
    'button[is="emby-tab-button"]',
    '.libraryPage .emby-tabs-button',
    '.libraryPage button[role="tab"]',
    '.headerTabs button',
    '.emby-tabs button',
    '.libraryPage [role="tablist"] button',
    'button.headerTab',
  ];
  function _findAllTabButtons() {
    const seen = new Set();
    const out = [];
    for (const sel of _TAB_BUTTON_SELECTORS) {
      document.querySelectorAll(sel).forEach(btn => {
        if (!seen.has(btn)) { seen.add(btn); out.push(btn); }
      });
    }
    return out;
  }
  function _fireClick(el) {
    // Synthesize the full mouse cycle — some Jellyfin Web tab handlers
    // listen on mousedown rather than click, so a bare .click() misses.
    ['mousedown', 'mouseup', 'click'].forEach(type => {
      try {
        el.dispatchEvent(new MouseEvent(type, {
          bubbles: true, cancelable: true, view: window, button: 0,
        }));
      } catch (_) { /* ignore */ }
    });
  }
  function _setHashTab(index) {
    const hash = location.hash || '';
    let next;
    if (/[?&]tab=\d+/.test(hash)) {
      next = hash.replace(/([?&])tab=\d+/, '$1tab=' + index);
    } else {
      const sep = hash.includes('?') ? '&' : '?';
      next = hash + sep + 'tab=' + index;
    }
    if (next !== hash) location.hash = next;
  }
  window.__jellytoast_switch_tab = function(label, index) {
    const target = (label || '').trim().toLowerCase();
    const buttons = _findAllTabButtons();
    console.log('[JellyToast] switch_tab:', label, 'index:', index,
                '— found', buttons.length, 'tab buttons');
    if (target) {
      for (const btn of buttons) {
        const txt = (btn.textContent || '').trim().toLowerCase();
        if (txt === target) {
          console.log('[JellyToast] clicking matched tab button:', txt);
          _fireClick(btn);
          return true;
        }
      }
    }
    if (typeof index === 'number' && index >= 0 && index < buttons.length) {
      console.log('[JellyToast] no label match — clicking by index', index);
      _fireClick(buttons[index]);
      return true;
    }
    if (typeof index === 'number' && index >= 0) {
      console.log('[JellyToast] no buttons matched — falling back to URL ?tab=' + index);
      _setHashTab(index);
      return true;
    }
    console.warn('[JellyToast] could not switch to tab:', label);
    return false;
  };

  // Stamp a timestamp every time the user clicks anything that looks
  // like a Shuffle button. Python reads this stamp via __jellytoast_
  // queue_state and, if the click was within the last 3s, forces a
  // library-wide shuffle (overriding Jellyfin Web's "shuffle one
  // album" behavior). Captured in the capture phase so we observe
  // the click even if Jellyfin's own handler stops propagation.
  window.__jellytoast_shuffle_clicked_at = 0;
  // Push the shuffle event to Python directly via the QWebChannel
  // bridge — saves ~250ms of JF Web round-trip (metadata fetch +
  // audio request + intercept + queue-state callback) before library
  // shuffle starts. The intercept-driven path stays as a fallback for
  // when the bridge isn't ready yet at click time.
  function notifyShuffle(via) {
    window.__jellytoast_shuffle_clicked_at = Date.now();
    console.log('[JellyToast] shuffle button clicked (' + via + ')');
    try {
      if (window.jellytoast
          && typeof window.jellytoast.shuffleClicked === 'function') {
        window.jellytoast.shuffleClicked();
      }
    } catch (_) { /* bridge not ready; stamp will be used by intercept path */ }
  }
  // Track the timestamp of the user's most recent click anywhere on the
  // JF Web page. Python uses this to distinguish a deliberate album/
  // play click (fresh stamp) from JF Web's silent auto-advance retries
  // after we silence its player (no click — driven by <audio> events).
  // Capture-phase listener so we always see clicks even if a deeper
  // handler stops propagation.
  window.__jellytoast_last_click_at = 0;
  // Most-recently-clicked tile's identity (data-id + data-type). The
  // queue_state payload includes this so Python can attribute the
  // play intent back to its source — e.g. clicking the center play
  // overlay on a Playlist tile installs as PLAYLIST even when the
  // playlist's tracks all happen to share an AlbumId (a "playlist
  // that mirrors one album", which the AlbumId-uniformity heuristic
  // would otherwise misclassify as an album play).
  window.__jellytoast_last_play_source = null;
  function captureTileClick(target) {
    // JF Web tiles always carry data-id; data-type is inconsistent
    // (some card variants strip it). Walk up looking for the OUTERMOST
    // data-id ancestor — playlist/album tiles wrap inner buttons that
    // may also carry data-id (the inner ID often points to a track
    // inside the collection rather than the collection itself). We
    // want the tile's own data-id, which is the rootmost match.
    var el = target;
    var bestId = null, bestType = null, bestName = null;
    for (var i = 0; el && i < 16; i++, el = el.parentElement) {
      if (!el.getAttribute) continue;
      var id = el.getAttribute('data-id');
      if (id) {
        bestId = id;
        bestType = el.getAttribute('data-type') || bestType;
        bestName = el.getAttribute('data-name')
                  || el.getAttribute('aria-label') || bestName;
      }
    }
    if (bestId) {
      window.__jellytoast_last_play_source = {
        id: bestId, type: bestType || '', name: bestName || '',
        clicked_at: Date.now(),
      };
    }
  }
  document.addEventListener('click', function(e) {
    window.__jellytoast_last_click_at = Date.now();
    captureTileClick(e.target);
  }, true);
  document.addEventListener('keydown', function(e) {
    // Treat Enter / Space as click-equivalent so keyboard activation
    // of a focused play button still registers as a user-driven event.
    if (e.key === 'Enter' || e.key === ' ' || e.key === 'Spacebar') {
      window.__jellytoast_last_click_at = Date.now();
      captureTileClick(e.target);
    }
  }, true);

  (function installShuffleClickHook() {
    var SHUFFLE_MATCHERS = [
      '.btnShuffle',
      '.btnShuffleAll',
      'button[is="paper-icon-button-light"].btnShuffle',
      'button[title*="Shuffle" i]',
      'button[aria-label*="Shuffle" i]',
      '[data-action*="shuffle" i]',
    ];
    document.addEventListener('click', function(e) {
      var el = e.target;
      for (var i = 0; el && i < 8; i++, el = el.parentElement) {
        if (!el.matches) continue;
        for (var j = 0; j < SHUFFLE_MATCHERS.length; j++) {
          try {
            if (el.matches(SHUFFLE_MATCHERS[j])) {
              notifyShuffle('matched ' + SHUFFLE_MATCHERS[j]);
              return;
            }
          } catch (_) { /* invalid selector — skip */ }
        }
        // Fall back: a span.material-icons.shuffle inside any button.
        if (el.tagName === 'BUTTON' || el.tagName === 'A') {
          var icon = el.querySelector
            && el.querySelector('.material-icons.shuffle, [class*="shuffle" i]');
          if (icon) {
            notifyShuffle('icon descendant');
            return;
          }
        }
      }
    }, true);
  })();

  // Snapshot Jellyfin Web's current playback queue + index plus a
  // shuffle-intent flag. Returned as JSON-encoded {items, index,
  // shuffle} or null if the manager isn't ready. Used right after
  // a /Audio/{id}/stream interception so Python can decide whether
  // to use Jellyfin Web's queue, override with library shuffle,
  // or fall back to manual context expansion. The shuffle stamp is
  // consumed on first read — JF Web error-advances through its own
  // queue after we block playback, generating extra intent fires;
  // we don't want those re-triggering library shuffle.
  window.__jellytoast_queue_state = function() {
    var stamp = window.__jellytoast_shuffle_clicked_at || 0;
    var shuffleIntent = (Date.now() - stamp) < 3000;
    if (shuffleIntent) window.__jellytoast_shuffle_clicked_at = 0;
    var lastClick = window.__jellytoast_last_click_at || 0;
    var clickAgeMs = lastClick > 0 ? (Date.now() - lastClick) : 999999;
    try {
      var pm = window.playbackManager;
      if (!pm || typeof pm.playlist !== 'function') {
        return JSON.stringify({
          items: null, index: 0,
          shuffle: shuffleIntent, click_age_ms: clickAgeMs,
        });
      }
      var list = pm.playlist();
      if (!list || !list.length) {
        return JSON.stringify({
          items: null, index: 0,
          shuffle: shuffleIntent, click_age_ms: clickAgeMs,
        });
      }
      var idx = (typeof pm.currentPlaylistIndex === 'function')
        ? pm.currentPlaylistIndex() : 0;
      var items = list.map(function(it) {
        return {
          Id: it.Id, Name: it.Name, Type: it.Type,
          Album: it.Album, AlbumId: it.AlbumId,
          AlbumPrimaryImageTag: it.AlbumPrimaryImageTag,
          AlbumArtist: it.AlbumArtist, Artists: it.Artists,
          ArtistItems: it.ArtistItems,
          RunTimeTicks: it.RunTimeTicks,
          IndexNumber: it.IndexNumber,
          ParentIndexNumber: it.ParentIndexNumber,
          ImageTags: it.ImageTags,
          MediaType: it.MediaType,
          UserData: it.UserData,
        };
      });
      var src = window.__jellytoast_last_play_source;
      // Stale guard — only surface the source attribution if it was
      // captured recently. A 5s window is generous enough to absorb
      // the JF Web round-trip (metadata fetch → audio request →
      // intercept → queue_state callback) but tight enough to avoid
      // attributing a fresh play to an unrelated old tile click.
      if (src && (Date.now() - src.clicked_at) > 5000) {
        src = null;
      }
      return JSON.stringify({
        items: items, index: idx,
        shuffle: shuffleIntent, click_age_ms: clickAgeMs,
        source_id: src ? src.id : '',
        source_type: src ? src.type : '',
        source_name: src ? src.name : '',
      });
    } catch (e) {
      console.warn('[JellyToast] queue_state error:', e);
      return JSON.stringify({
        items: null, index: 0,
        shuffle: shuffleIntent, click_age_ms: clickAgeMs,
      });
    }
  };

  // Stop Jellyfin Web's audio dead. Called by Python every time we
  // install our own queue — without this, JF Web's player error-
  // advances through *its* queue (the album-shuffle's tracks, the
  // currently-displayed library, etc.) and each retry generates a
  // /Audio/{id}/... request that our interceptor catches. Once the
  // cooldown lifts, one of those expansions can overwrite our queue.
  //
  // Two layers of attack so this works regardless of JF Web version:
  //
  //  1. Pause + clear every <audio>/<video> element in the DOM. The
  //     auto-advance triggers off the media element's `error`/`ended`
  //     events; with `src` cleared and load() called, the element
  //     stops firing those, so JF Web has nothing to advance from.
  //     This is the version-proof path — JF Web 10.11.7 dropped the
  //     `window.playbackManager` global, but every browser HTMLAudio
  //     player still goes through <audio>.
  //
  //  2. Best-effort `pm.stop()` for older JF Web versions where the
  //     manager is still on `window`. No-op on 10.11.7.
  window.__jellytoast_silence_jfweb = function() {
    try {
      var media = document.querySelectorAll('audio, video');
      var n = 0;
      media.forEach(function(el) {
        try {
          el.pause();
          // Detaching src prevents the next-track request the player
          // would otherwise queue when this element errors.
          el.removeAttribute('src');
          // load() with no src tears down the resource selection
          // algorithm — kills the readyState transitions JF Web's
          // playback hooks listen for.
          try { el.load(); } catch (e) {}
          n++;
        } catch (e) { /* per-element; keep going */ }
      });
      var pm = window.playbackManager;
      if (pm && typeof pm.stop === 'function') {
        try { pm.stop(); } catch (e) {}
        ['_playlist', '_currentPlaylistIndex'].forEach(function(k) {
          if (pm[k] !== undefined) {
            if (Array.isArray(pm[k])) pm[k].length = 0;
            else pm[k] = -1;
          }
        });
      }
      console.log('[JellyToast] silenced ' + n + ' media element(s)');
    } catch (e) {
      console.warn('[JellyToast] silence error:', e);
    }
  };

  // Returns the label of the currently-active tab, or '' if no tab
  // strip is rendered. Used by Python to keep the View dropdown's
  // label in sync with whatever Jellyfin Web is showing.
  window.__jellytoast_active_tab = function() {
    const sels = [
      'button.emby-tabs-button.is-active',
      'button[is="emby-tab-button"].is-active',
      'button.emby-tabs-button[aria-selected="true"]',
      'button[is="emby-tab-button"][aria-selected="true"]',
      '.libraryPage .emby-tabs-button.is-active',
      '.headerTabs button.is-active',
    ];
    for (const sel of sels) {
      const btn = document.querySelector(sel);
      if (btn) {
        const t = (btn.textContent || '').trim();
        if (t) return t;
      }
    }
    return '';
  };
  // Detect the current library's collectionType from the URL hash
  // (#/music?…&collectionType=music) so the native top bar can show
  // the right View dropdown items. Returns "" off library pages.
  window.__jellytoast_collection_type = function() {
    const m = (location.hash || '').match(/[?&]collectionType=([^&]+)/);
    return m ? decodeURIComponent(m[1]).toLowerCase() : '';
  };

  // Drawer toggle helper — JtTopBar's hamburger calls this via JS.
  // Jellyfin Web's actual drawer trigger lives inside .skinHeader (which
  // we now hide), but the underlying button still receives clicks if we
  // can find it. Try a few likely selectors before giving up.
  window.__jellytoast_toggle_drawer = function() {
    const sels = [
      '.headerButton.mainDrawerButton',
      '.mainDrawerButton',
      '.headerDrawerButton',
      'button.headerButton[title="Menu"]',
      'button[is="paper-icon-button-light"].mainDrawerButton',
    ];
    for (const s of sels) {
      const btn = document.querySelector(s);
      if (btn) { btn.click(); return true; }
    }
    return false;
  };

  function init() {
    injectCSS();
    if (document.body) { watchDialogs(); killTopPadding(); }
    else document.addEventListener('DOMContentLoaded', () => {
      watchDialogs(); killTopPadding();
    });
  }

  init();
  bind();
})();
"""


class Bridge(QObject):
    """JS→Python calls. Wired through QWebChannel as `window.jellytoast`."""

    shuffle_requested = Signal()
    page_ready = Signal()
    page_rendered = Signal()
    # JF Web is the source of truth for auth — the user signs in through
    # its UI, which stores the session in localStorage. Python never sees
    # those credentials otherwise; without bridging them across, every
    # api.* call that needs `/Users/{user_id}/...` 404s on a double-slash
    # URL and intent fallback breaks.
    credentials_received = Signal(str, str, str)  # server_url, user_id, token

    @Slot(str)
    def diagnostic(self, msg: str):
        print(f"[JellyToast/JS] {msg}", flush=True)

    @Slot()
    def shuffleClicked(self):
        # Fired the instant the JS click hook detects a shuffle button
        # press — lets us start library shuffle immediately instead of
        # waiting for JF Web's metadata + audio-request round-trip.
        self.shuffle_requested.emit()

    @Slot()
    def pageReady(self):
        # Fired by __jellytoast_reveal() when the page DOM is laid out.
        # Tells the host it's safe to show the window — JF Web has
        # finished bootstrapping, the music library has navigated, and
        # the album cards are present in the DOM.
        self.page_ready.emit()

    @Slot()
    def pageRendered(self):
        # Fired after the page has actually composited to screen
        # (visibilitystate went visible + a few rAF callbacks fired
        # at full frame rate). Tells the host to hide the loading
        # overlay — at this point chrome and album content are both
        # painted, so they appear together rather than in stages.
        self.page_rendered.emit()

    @Slot(str, str, str)
    def setCredentials(self, server_url: str, user_id: str, token: str):
        # JF Web pushes its current ApiClient credentials over the bridge
        # whenever sign-in state changes. The host caches them so Python
        # can call /Users/{user_id}/... endpoints (library views, item
        # metadata, queue fallbacks).
        if server_url and user_id and token:
            self.credentials_received.emit(server_url, user_id, token)


class _LoggingPage(QWebEnginePage):
    """Routes Jellyfin Web's JS console to the terminal."""
    _LEVEL_NAMES = {0: "INFO", 1: "WARN", 2: "ERROR"}

    def javaScriptConsoleMessage(self, level, message, line, source):
        try:
            lvl = level.value if hasattr(level, "value") else int(level)
        except Exception:
            lvl = 0
        name = self._LEVEL_NAMES.get(lvl, str(lvl))
        src = source.rsplit("/", 1)[-1] if source else "?"
        print(f"[js {name}] {message}  ({src}:{line})", flush=True)
