/* Module Soleil — chip lever/coucher dans la top bar + popup détails.
   S'injecte après #gps-indicator (à côté du chip météo) sans toucher index.html. */
(function () {

    /* --- Styles (réutilise les variables CSS de thème) --- */
    const style = document.createElement('style');
    style.textContent = `
        #sun-chip {
            font-size:0.85em; font-weight:bold;
            padding:3px 10px; border-radius:6px;
            border:1px solid var(--c-border);
            color:var(--c-muted);
            cursor:pointer; user-select:none;
            display:none; position:relative;
        }
        #sun-popup {
            display:none; position:fixed; top:38px; left:50%;
            transform:translateX(-50%);
            background:var(--c-panel);
            border:2px solid var(--c-border);
            border-radius:10px;
            padding:10px 18px; z-index:9999;
            color:var(--c-text);
            font-family:"Courier New",monospace;
            font-size:0.9em;
            box-shadow:0 4px 16px rgba(0,0,0,0.4);
            min-width:220px; text-align:center;
        }
        .sp-title { margin-bottom:8px; font-size:1.05em; }
        .sp-row   { display:flex; justify-content:space-between; gap:18px; margin:3px 0; }
        .sp-lbl   { color:var(--c-label); }
        .sp-val   { color:var(--c-text); }
        .sp-day   { color:var(--c-muted); font-size:0.8em; margin-top:8px; }
    `;
    document.head.appendChild(style);

    /* --- Chip dans la top bar --- */
    const chip = document.createElement('div');
    chip.id = 'sun-chip';
    chip.title = 'Soleil — cliquer pour lever/coucher et crépuscule';
    chip.textContent = '…';

    const gpsEl = document.getElementById('gps-indicator');
    if (gpsEl && gpsEl.parentNode) {
        gpsEl.parentNode.insertBefore(chip, gpsEl.nextSibling);
    }

    /* --- Popup détails --- */
    const popup = document.createElement('div');
    popup.id = 'sun-popup';
    document.body.appendChild(popup);

    let popupOpen = false;
    chip.addEventListener('click', function (e) {
        e.stopPropagation();
        popupOpen = !popupOpen;
        popup.style.display = popupOpen ? 'block' : 'none';
    });
    document.addEventListener('click', function () {
        popupOpen = false;
        popup.style.display = 'none';
    });

    function row(lbl, val) {
        return `<div class="sp-row"><span class="sp-lbl">${lbl}</span>`
             + `<span class="sp-val">${val || '—'}</span></div>`;
    }

    /* --- Poll /api/soleil toutes les 30s --- */
    async function refreshSun() {
        try {
            const d = await fetch('/api/soleil').then(r => r.json());
            if (!d.ok) {
                chip.style.display = 'none';
                return;
            }

            // Chip : ☀️/🌙 selon jour/nuit, puis lever 🌅 et coucher 🌇.
            if (d.polar === 'day') {
                chip.textContent = '☀️ soleil de minuit';
            } else if (d.polar === 'night') {
                chip.textContent = '🌙 nuit polaire';
            } else {
                const state = d.is_day ? '☀️' : '🌙';
                chip.textContent = `${state} 🌅 ${d.sunrise || '—'}  🌇 ${d.sunset || '—'}`;
            }
            chip.style.display = 'inline-block';

            let html = `<div class="sp-title"><strong>${d.is_day ? '☀️ Jour' : '🌙 Nuit'}</strong></div>`;
            html += row('🌅 Lever', d.sunrise);
            html += row('🌇 Coucher', d.sunset);
            html += row('🌆 Aube naut.', d.dawn_naut);
            html += row('🌌 Crép. naut.', d.dusk_naut);
            if (d.day_len) {
                html += `<div class="sp-day">Durée du jour : ${d.day_len}</div>`;
            }
            popup.innerHTML = html;

        } catch (e) {
            chip.style.display = 'none';
        }
    }

    refreshSun();
    setInterval(refreshSun, 30000);

})();
