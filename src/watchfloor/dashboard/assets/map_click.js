// Ways to clear the current selection besides the "Clear selection" button:
//   - click empty map (a click, not a pan; not on a point, the legend or map controls)
//   - press Esc (unless typing in a field, e.g. a review note)
// Both press the existing button, so the server handles every clear the same way.
(function () {
  var DRAG_PX = 5;
  var down = null;
  var lastPointClick = 0;

  function clearSelection() {
    var btn = document.getElementById("clear-selection");
    if (btn) btn.click();
  }

  function graphDiv() {
    return document.querySelector("#map .js-plotly-plot");
  }

  // Plotly reports clicks on points through its own event, not the DOM, so note them. Only
  // points that select something count (they carry customdata); a click on a trail line or
  // a cable is treated as empty map.
  function hookPointClicks() {
    var gd = graphDiv();
    if (gd && gd.on && !gd._wfHooked) {
      gd.on("plotly_click", function (ev) {
        var p = ev && ev.points && ev.points[0];
        if (p && p.customdata) lastPointClick = Date.now();
      });
      gd._wfHooked = true;
    }
  }

  document.addEventListener("mousedown", function (e) {
    if (!e.target.closest("#map")) return;
    hookPointClicks();
    down = { x: e.clientX, y: e.clientY };
  });

  document.addEventListener("click", function (e) {
    if (!e.target.closest("#map") || !down) return;
    var moved = Math.abs(e.clientX - down.x) + Math.abs(e.clientY - down.y) > DRAG_PX;
    down = null;
    if (moved) return; // a pan
    if (e.target.closest(".legend, .modebar, .maplibregl-ctrl, .maplibregl-ctrl-attrib")) return;
    var started = Date.now();
    // Plotly's point click fires during the same DOM click; check once it has had the chance.
    setTimeout(function () {
      if (lastPointClick >= started - 50) return; // clicked a point: let it select
      clearSelection();
    }, 60);
  });

  // "Show on map" in the reports list (below the console): bring the map back into view.
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("button[id]");
    if (btn && btn.id.indexOf('"report-open"') !== -1) {
      window.scrollTo({ top: 0, behavior: "smooth" });
    }
  });

  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    var t = e.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
    clearSelection();
  });
})();
