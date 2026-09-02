/* 板のドラッグ。ボタン (`board_columns.html`) が本体で、これは上乗せ。
 *
 * 委譲で書いてある: 板は 20 秒ごとに innerHTML ごと差し替わるので、
 * カードに直接張ったリスナは 1 回目の poll で全部消える。document に
 * 1 組だけ張り、そこから探す。
 *
 * poll との競合は `window.__boardDragging` で止める。board.html の
 * hx-trigger がこのフラグを見ていて、掴んでいる間だけ描き直しが止まる
 * —— 掴んだカードが手の中で消えるのは、UI の不具合ではなく操作の喪失。
 */
(function () {
    "use strict";

    var DRAG_TYPE = "text/plain";
    var dragging = null;

    window.__boardDragging = false;

    function card(el) {
        return el && el.closest ? el.closest(".board-card[draggable='true']") : null;
    }

    function zone(el) {
        return el && el.closest ? el.closest("[data-dropzone]") : null;
    }

    function clearHover() {
        var hovered = document.querySelectorAll(".board-col-body.is-drop-target");
        for (var i = 0; i < hovered.length; i++) {
            hovered[i].classList.remove("is-drop-target");
        }
    }

    document.addEventListener("dragstart", function (e) {
        var el = card(e.target);
        if (!el) return;
        dragging = el;
        window.__boardDragging = true;
        el.classList.add("is-dragging");
        // Firefox はデータを載せないと drag を始めない。中身は使わない
        // (要素は `dragging` で持っている) が、空だと無反応になる。
        if (e.dataTransfer) {
            e.dataTransfer.effectAllowed = "move";
            try {
                e.dataTransfer.setData(DRAG_TYPE, el.dataset.itemKey || "");
            } catch (err) { /* 古い実装。載せられなくても続行する */ }
        }
    });

    document.addEventListener("dragend", function () {
        if (dragging) dragging.classList.remove("is-dragging");
        dragging = null;
        window.__boardDragging = false;
        clearHover();
    });

    document.addEventListener("dragover", function (e) {
        if (!dragging) return;
        var target = zone(e.target);
        if (!target) return;
        // preventDefault しない限りブラウザは drop を発火させない。
        e.preventDefault();
        if (e.dataTransfer) e.dataTransfer.dropEffect = "move";
        if (!target.classList.contains("is-drop-target")) {
            clearHover();
            target.classList.add("is-drop-target");
        }
    });

    document.addEventListener("drop", function (e) {
        if (!dragging) return;
        var target = zone(e.target);
        if (!target) return;
        e.preventDefault();

        var lane = target.getAttribute("data-dropzone");
        var el = dragging;
        // dragend は drop の後に来る。先に掴んだ状態を畳んでおかないと、
        // POST が返るまでフラグが立ったままになり poll が止まり続ける。
        dragging = null;
        window.__boardDragging = false;
        el.classList.remove("is-dragging");
        clearHover();

        if (!lane || lane === el.dataset.lane) return;

        // 移動はボタンと同じ 1 本の口を通す。ここで fetch を書くと、
        // 同じ操作に 2 つの実装ができて片方だけ直る日が来る。
        htmx.ajax("POST", "/dashboard/decisions/_lane", {
            target: "#board",
            swap: "innerHTML",
            values: {
                item_key: el.dataset.itemKey || "",
                lane: lane,
                fingerprint: el.dataset.fingerprint || ""
            }
        });
    });
})();
