/**
 * 增强型文件多选上传组件 — 图片视频统一支持。
 *
 * 特性：
 *   - 每次选择追加到已有列表（而非替换）
 *   - 即时缩略图网格预览（图片直显 / 视频显示文件名+尺寸块）
 *   - 每个预览项带 × 删除按钮（真删，不是只删 DOM — 重建 input.files）
 *   - 点击预览项可查看大图 / 播放视频（弹层，支持 Esc / 遮罩关闭）
 *   - 保留原生 <input type="file" multiple required>，不破坏 HTML5 校验
 *   - 文件大小前端校验（图片 ≤20MB，视频 ≤500MB）
 *   - URL.createObjectURL 内存管理：每次 render 先 revoke 旧 URL，删除单独 revoke
 *
 * 使用：enhanceFileInput(document.querySelector('input[type=file][name=media]'));
 *       或直接给 input 加 data-enhance="media"，DOMContentLoaded 自动增强
 */
(function () {
    'use strict';

    function fmtSize(bytes) {
        if (bytes < 1024) return bytes + ' B';
        if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
        return (bytes / 1024 / 1024).toFixed(1) + ' MB';
    }

    function isImage(file) { return file.type.startsWith('image/'); }
    function isVideo(file) { return file.type.startsWith('video/'); }
    function isAccepted(file) { return isImage(file) || isVideo(file); }

    function enhanceFileInput(input, opts) {
        opts = opts || {};
        var MAX_IMG = opts.maxImageMb != null ? opts.maxImageMb * 1024 * 1024 : 20 * 1024 * 1024;
        var MAX_VID = opts.maxVideoMb != null ? opts.maxVideoMb * 1024 * 1024 : 500 * 1024 * 1024;

        var files = [];       // 持久化 File 对象数组
        var activeURLs = new Set(); // 所有当前激活的 objectURL（render 前统一 revoke）

        // ---- 1. 隐藏原生 input，构建 UI 外壳 ----
        input.style.display = 'none';

        var wrapper = document.createElement('div');
        wrapper.className = 'fu-wrapper';
        input.parentNode.insertBefore(wrapper, input);
        wrapper.appendChild(input);

        var btnRow = document.createElement('div');
        btnRow.className = 'fu-btn-row';
        btnRow.style.cssText = 'display:flex;gap:10px;align-items:center;margin:10px 0 12px;';

        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'btn btn-secondary btn-sm';
        btn.textContent = '选择文件';
        btnRow.appendChild(btn);

        var addBtn = document.createElement('button');
        addBtn.type = 'button';
        addBtn.className = 'btn btn-sm';
        addBtn.style.background = '#417690';
        addBtn.textContent = '+ 继续添加';
        addBtn.style.display = 'none';
        btnRow.appendChild(addBtn);

        var countLabel = document.createElement('span');
        countLabel.className = 'muted';
        countLabel.style.fontSize = '13px';
        btnRow.appendChild(countLabel);

        wrapper.appendChild(btnRow);

        var grid = document.createElement('div');
        grid.className = 'fu-grid';
        grid.style.cssText = 'display:flex;flex-wrap:wrap;gap:12px;';
        wrapper.appendChild(grid);

        var msg = document.createElement('div');
        msg.className = 'fu-msg';
        msg.style.cssText = 'margin-top:6px;font-size:13px;color:#7f8c99;min-height:18px;';
        wrapper.appendChild(msg);

        var lightbox = null;

        // ---- 2. 把 JS files 数组同步回 input.files（DataTransfer） ----
        function syncInputFiles() {
            var dt = new DataTransfer();
            for (var i = 0; i < files.length; i++) dt.items.add(files[i]);
            input.files = dt.files;
            if (input.hasAttribute('required')) {
                input.setCustomValidity(files.length === 0 ? '请至少上传一个文件' : '');
            }
        }

        function revokeAllURLs() {
            activeURLs.forEach(function (u) { try { URL.revokeObjectURL(u); } catch (_) {} });
            activeURLs.clear();
        }

        // ---- 3. 渲染缩略图网格 ----
        function render() {
            // ★ 关键修复：每次重绘前先释放所有旧 URL，避免内存泄漏
            revokeAllURLs();
            grid.innerHTML = '';

            for (var i = 0; i < files.length; i++) {
                (function (idx) {
                    var file = files[idx];
                    var card = document.createElement('div');
                    card.className = 'fu-card';
                    card.style.cssText = [
                        'width:160px;height:160px;border:1px solid #e3edf3;border-radius:8px;',
                        'overflow:hidden;background:#fafbfc;position:relative;cursor:pointer;',
                        'transition:border-color .15s, box-shadow .15s;',
                    ].join('');
                    card.addEventListener('mouseenter', function () {
                        card.style.borderColor = '#417690';
                        card.style.boxShadow = '0 0 0 2px rgba(65,118,144,.15)';
                    });
                    card.addEventListener('mouseleave', function () {
                        card.style.borderColor = '#e3edf3';
                        card.style.boxShadow = 'none';
                    });

                    if (isImage(file)) {
                        var url = URL.createObjectURL(file);
                        activeURLs.add(url);
                        var img = document.createElement('img');
                        img.src = url;
                        img.style.cssText = 'width:100%;height:110px;object-fit:cover;display:block;background:#eee;';
                        img.draggable = false;
                        card.appendChild(img);
                    } else if (isVideo(file)) {
                        var vWrap = document.createElement('div');
                        vWrap.style.cssText = [
                            'width:100%;height:110px;background:#1e2b33;display:flex;',
                            'align-items:center;justify-content:center;color:#fff;font-size:13px;',
                        ].join('');
                        vWrap.innerHTML = '▶<span style="margin-left:6px;">视频</span>';
                        card.appendChild(vWrap);
                    }

                    var info = document.createElement('div');
                    info.style.cssText = [
                        'padding:6px 8px;font-size:12px;line-height:1.3;',
                        'color:#48657a;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;',
                    ].join('');
                    info.title = file.name;
                    info.textContent = file.name;
                    var sizeSpan = document.createElement('div');
                    sizeSpan.style.cssText = 'color:#95a5a6;font-size:11px;margin-top:2px;';
                    sizeSpan.textContent = fmtSize(file.size);
                    info.appendChild(sizeSpan);
                    card.appendChild(info);

                    var del = document.createElement('button');
                    del.type = 'button';
                    del.setAttribute('aria-label', '删除');
                    del.style.cssText = [
                        'position:absolute;top:4px;right:4px;width:22px;height:22px;',
                        'border-radius:50%;background:rgba(0,0,0,.55);color:#fff;border:none;',
                        'cursor:pointer;font-size:14px;line-height:1;padding:0;display:flex;',
                        'align-items:center;justify-content:center;',
                    ].join('');
                    del.textContent = '×';
                    del.addEventListener('click', function (e) {
                        e.stopPropagation();
                        removeAt(idx);
                    });
                    card.appendChild(del);

                    card.addEventListener('click', function () { openLightbox(file); });

                    grid.appendChild(card);
                })(i);
            }

            countLabel.textContent = files.length ? '共 ' + files.length + ' 个文件' : '尚未选择文件';
            addBtn.style.display = files.length ? '' : 'none';
            btn.textContent = files.length ? '重新选择（替换全部）' : '选择文件';
        }

        function removeAt(idx) {
            // 先 release render 持有的 URL（render 会在最后调用）
            var urlToRelease = null;
            // 用 find 找哪个 active url 属于此文件（render 会重建，所以当前 render 的 URL 都属于仍在 files 里的项）
            // 因为紧接着要 render()，它会一次性 revoke 全部再重建，这里只需 splice
            files.splice(idx, 1);
            syncInputFiles();
            render(); // render 会统一 revoke 旧的 + 重建新的
        }

        function clearAll() {
            files = [];
            syncInputFiles();
            render(); // render 统一 revoke
        }

        // ---- 4. 文件选择处理 ----
        function handleSelection(e, mode) {
            var selected = Array.prototype.slice.call(e.target.files || []);
            if (!selected.length) return;

            var accepted = [], rejected = [];
            for (var i = 0; i < selected.length; i++) {
                var f = selected[i];
                if (!isAccepted(f)) { rejected.push(f.name + '（非图片/视频）'); continue; }
                if (isImage(f) && f.size > MAX_IMG) { rejected.push(f.name + '（图片超过 20MB）'); continue; }
                if (isVideo(f) && f.size > MAX_VID) { rejected.push(f.name + '（视频超过 500MB）'); continue; }
                accepted.push(f);
            }

            if (mode === 'replace') clearAll();  // clearAll 内部会 render（revoke+重建）

            Array.prototype.push.apply(files, accepted);

            // ★ 顺序关键：先重置原生 value（为了下次选同文件也触发 change），
            // 再用我们内部的 files 重建 input.files，否则 value='' 会把刚重建的又清掉
            input.value = '';
            syncInputFiles();
            render();

            if (rejected.length) {
                msg.style.color = '#d9534f';
                msg.textContent = '已跳过 ' + rejected.length + ' 个不符合要求的文件：' + rejected.slice(0, 3).join('、') +
                    (rejected.length > 3 ? ' 等' : '');
            } else {
                msg.textContent = '';
            }
        }

        btn.addEventListener('click', function () {
            input.setAttribute('data-mode', 'replace');
            input.click();
        });
        addBtn.addEventListener('click', function () {
            input.setAttribute('data-mode', 'append');
            input.click();
        });
        input.addEventListener('change', function (e) {
            var mode = input.getAttribute('data-mode') === 'append' ? 'append' : 'replace';
            handleSelection(e, mode);
        });

        // ---- 5. 轻量级弹层：图片大图 / 视频播放 ----
        function openLightbox(file) {
            if (lightbox) return;
            lightbox = document.createElement('div');
            lightbox.style.cssText = [
                'position:fixed;inset:0;background:rgba(0,0,0,.85);z-index:9999;',
                'display:flex;align-items:center;justify-content:center;cursor:zoom-out;',
            ].join('');

            var closeBtn = document.createElement('button');
            closeBtn.type = 'button';
            closeBtn.textContent = '× 关闭';
            closeBtn.style.cssText = [
                'position:absolute;top:14px;right:14px;background:rgba(255,255,255,.2);',
                'color:#fff;border:1px solid rgba(255,255,255,.3);border-radius:4px;',
                'padding:6px 14px;font-size:14px;cursor:pointer;',
            ].join('');

            var media;
            if (isImage(file)) {
                media = document.createElement('img');
                media.src = URL.createObjectURL(file);
                media.style.cssText = [
                    'max-width:92vw;max-height:88vh;border-radius:6px;box-shadow:0 6px 30px rgba(0,0,0,.5);',
                ].join('');
            } else {
                media = document.createElement('video');
                media.src = URL.createObjectURL(file);
                media.controls = true;
                media.muted = true;   // ★ 自动静音才能 autoplay
                media.autoplay = true;
                media.style.cssText = [
                    'max-width:92vw;max-height:88vh;border-radius:6px;background:#000;',
                ].join('');
            }

            var lbURLs = [media.src];
            lightbox.appendChild(media);
            lightbox.appendChild(closeBtn);
            document.body.appendChild(lightbox);

            function close() {
                if (!lightbox) return;
                lbURLs.forEach(function (u) { try { URL.revokeObjectURL(u); } catch (_) {} });
                document.body.removeChild(lightbox);
                lightbox = null;
                document.removeEventListener('keydown', keyHandler);
            }
            lightbox.addEventListener('click', function (e) { if (e.target === lightbox) close(); });
            closeBtn.addEventListener('click', close);
            function keyHandler(e) { if (e.key === 'Escape') close(); }
            document.addEventListener('keydown', keyHandler);
        }

        render();  // 初始渲染

        // 对外暴露：方便外部 reset
        input.__fu = { clear: clearAll };
    }

    document.addEventListener('DOMContentLoaded', function () {
        document.querySelectorAll('input[type=file][data-enhance="media"]').forEach(function (el) {
            enhanceFileInput(el);
        });
    });

    window.enhanceFileInput = enhanceFileInput;
})();
