/* The API owns document state. Never persist a second document list in the browser. */
"use strict";
(() => {
  const el = (id) => document.getElementById(id);
  const state = {
    offset: 0, limit: 20, total: 0, uploadBusy: false, listLoading: false,
    listRequest: 0, listController: null, selectedId: null,
    sourceRequest: 0, sourceController: null, sourceUrl: null,
    reviewRequest: 0, reviewController: null, reviewBusy: false, reviewLoading: false,
    document: null, extraction: null, dirty: false, reviewNeedsRefresh: false,
  };
  const statuses = {
    uploaded: "Yüklendi", classifying: "Tür belirleniyor", extracting: "Veri çıkarılıyor",
    extracted: "Veri çıkarıldı", matching: "Eşleştiriliyor", matched: "Eşleştirildi",
    pushed: "QBO’ya aktarıldı", failed: "Hata",
  };
  const types = {
    receipt: "Fiş", invoice: "Fatura", bill: "Fatura", bank_statement: "Banka ekstresi",
    check: "Çek", credit_card_statement: "Kredi kartı ekstresi", unknown: "Bilinmiyor",
  };
  const mediaByExtension = {pdf: "application/pdf", png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", heic: "image/heic"};
  const label = (mapping, value, fallback = "—") => mapping[value] || value || fallback;
  const node = (tag, text, className) => {
    const item = document.createElement(tag);
    if (text !== undefined) item.textContent = text;
    if (className) item.className = className;
    return item;
  };
  const pathFor = (id) => `/api/documents/${encodeURIComponent(id)}`;

  class APIError extends Error {
    constructor(message, status) { super(message); this.status = status; }
  }
  async function request(url, options = {}) {
    const response = await fetch(url, {cache: "no-store", ...options});
    if (!response.ok) {
      let detail = `İstek başarısız (HTTP ${response.status}).`;
      try {
        const data = await response.json();
        if (typeof data.detail === "string") detail = data.detail;
        else if (data.detail && typeof data.detail === "object" && !Array.isArray(data.detail)) detail = data.detail.message || detail;
        else if (Array.isArray(data.detail)) detail = data.detail.map((entry) => entry.msg || "Geçersiz veri").join("; ");
      } catch (_) { /* A proxy may return a non-JSON error body. */ }
      throw new APIError(detail, response.status);
    }
    return response;
  }
  async function json(url, options) { return (await request(url, options)).json(); }
  function errorMessage(error) {
    return error instanceof APIError ? error.message : "Sunucuya ulaşılamadı. Bağlantını ve sunucunun çalıştığını kontrol et.";
  }
  function pagination() {
    el("previous-page").disabled = state.listLoading || state.offset === 0;
    el("next-page").disabled = state.listLoading || state.offset + state.limit >= state.total;
    el("refresh-list").disabled = state.listLoading;
    el("page-info").textContent = state.total ? `${state.offset + 1}–${Math.min(state.offset + state.limit, state.total)} / ${state.total}` : "0 belge";
  }
  function selectRow() {
    for (const row of el("documents-body").children) row.classList.toggle("selected", row.dataset.id === state.selectedId);
  }
  function renderDocuments(items) {
    el("documents-body").replaceChildren();
    for (const document of items) {
      const row = node("tr");
      row.dataset.id = document.id;
      const file = node("td");
      file.append(node("span", document.filename, "file-name"), node("span", document.id, "document-id"));
      const status = node("td");
      const badge = node("span", label(statuses, document.status), "badge");
      if (Object.hasOwn(statuses, document.status)) badge.classList.add(document.status);
      if (document.error) badge.title = document.error;
      status.append(badge);
      const type = node("td", label(types, document.document_type, "Henüz belirlenmedi"), "type-label");
      const actions = node("td");
      const open = node("button", "Kaynağı aç", "button secondary row-action");
      open.type = "button";
      open.dataset.documentOpen = "true";
      open.disabled = state.reviewBusy;
      open.setAttribute("aria-label", `${document.filename} kaynağını aç`);
      open.addEventListener("click", () => openSource(document.id));
      actions.append(open);
      row.append(file, status, type, actions);
      el("documents-body").append(row);
    }
    selectRow();
  }
  async function loadDocuments(offset = state.offset) {
    state.listController?.abort();
    const controller = new AbortController();
    state.listController = controller;
    const sequence = ++state.listRequest;
    state.listLoading = true;
    el("list-error").hidden = true;
    el("list-message").hidden = false;
    el("list-message").textContent = "Belgeler yükleniyor…";
    el("documents-table").hidden = true;
    pagination();
    try {
      const query = new URLSearchParams({company_id: "default", limit: String(state.limit), offset: String(offset)});
      const data = await json(`/api/documents?${query}`, {signal: controller.signal});
      if (sequence !== state.listRequest) return;
      if (!Array.isArray(data.items) || !Number.isInteger(data.total)) throw new APIError("Belge listesi yanıtı okunamadı.");
      if (offset > 0 && offset >= data.total) {
        return await loadDocuments(data.total ? Math.floor((data.total - 1) / state.limit) * state.limit : 0);
      }
      state.offset = offset;
      state.total = data.total;
      renderDocuments(data.items);
      el("document-count").textContent = `${data.total} kayıtlı belge`;
      el("documents-table").hidden = data.items.length === 0;
      el("list-message").hidden = data.items.length > 0;
      el("list-message").textContent = "Henüz belge yok. İlk belgeni yukarıdan yükleyebilirsin.";
    } catch (error) {
      if (error.name === "AbortError" || sequence !== state.listRequest) return;
      el("list-message").hidden = true;
      el("list-error").textContent = errorMessage(error);
      el("list-error").hidden = false;
      el("document-count").textContent = "Liste yüklenemedi";
    } finally {
      if (sequence === state.listRequest) {
        state.listLoading = false;
        pagination();
      }
    }
  }

  function releaseSource() {
    // Destroy the old viewer before releasing the URL it references.
    el("source-viewer").replaceChildren();
    if (state.sourceUrl) URL.revokeObjectURL(state.sourceUrl);
    state.sourceUrl = null;
    el("source-download").removeAttribute("href");
    el("source-download").hidden = true;
  }
  function showSourceMessage(message) {
    el("source-viewer").replaceChildren(node("p", message, "muted"));
  }
  function detail(document) {
    const rows = [
      ["Belge ID", document.id], ["Durum", label(statuses, document.status)],
      ["Belge türü", label(types, document.document_type, "Henüz belirlenmedi")],
    ];
    if (document.qbo_transaction_id) rows.push(["QBO işlem ID", document.qbo_transaction_id]);
    if (document.error) rows.push(["İşleme hatası", document.error]);
    el("document-detail").replaceChildren();
    for (const [key, value] of rows) el("document-detail").append(node("dt", key), node("dd", value));
    el("document-detail").hidden = false;
  }
  async function openSource(id) {
    if (!canLeaveReview()) return;
    resetReviewState();
    state.sourceController?.abort();
    const controller = new AbortController();
    state.sourceController = controller;
    const sequence = ++state.sourceRequest;
    state.selectedId = id;
    selectRow();
    releaseSource();
    el("source-error").hidden = true;
    el("close-source").hidden = false;
    el("document-detail").hidden = true;
    el("source-filename").textContent = "Belge yükleniyor…";
    showSourceMessage("Kaynak dosya yükleniyor…");
    if (window.matchMedia("(max-width: 1050px)").matches) el("source-panel").scrollIntoView({behavior: "smooth", block: "start"});
    try {
      const document = await json(pathFor(id), {signal: controller.signal});
      if (sequence !== state.sourceRequest) return;
      el("source-filename").textContent = document.filename;
      detail(document);
      void loadReview(document, sequence);
      const response = await request(`${pathFor(id)}/source`, {signal: controller.signal});
      const blob = await response.blob();
      if (sequence !== state.sourceRequest) return;
      state.sourceUrl = URL.createObjectURL(blob);
      const download = el("source-download");
      download.href = state.sourceUrl;
      download.download = document.filename;
      download.hidden = false;
      const media = blob.type.split(";")[0].toLowerCase();
      if (media === "application/pdf") {
        const frame = node("iframe");
        frame.title = `${document.filename} PDF önizlemesi`;
        frame.src = state.sourceUrl;
        el("source-viewer").replaceChildren(frame);
      } else if (["image/png", "image/jpeg", "image/jpg"].includes(media)) {
        const image = node("img");
        image.alt = document.filename;
        image.addEventListener("error", () => {
          if (sequence === state.sourceRequest) showSourceMessage("Görsel önizlemesi açılamadı. Kaynak dosyayı indirerek kontrol edebilirsin.");
        });
        image.src = state.sourceUrl;
        el("source-viewer").replaceChildren(image);
      } else {
        showSourceMessage("Bu dosya türü tarayıcıda önizlenemiyor. Kaynak dosyayı indirerek açabilirsin.");
      }
    } catch (error) {
      if (error.name === "AbortError" || sequence !== state.sourceRequest) return;
      showSourceMessage("Kaynak dosya açılamadı.");
      el("source-error").textContent = errorMessage(error);
      el("source-error").hidden = false;
    }
  }
  function closeSource() {
    if (!canLeaveReview()) return;
    resetReviewState();
    ++state.sourceRequest;
    state.sourceController?.abort();
    state.selectedId = null;
    releaseSource();
    selectRow();
    el("source-filename").textContent = "Listeden bir belge seç.";
    el("document-detail").hidden = true;
    el("source-error").hidden = true;
    el("close-source").hidden = true;
    showSourceMessage("Seçtiğin belgenin kaynağı burada görüntülenir.");
  }

  const receiptFields = [
    ["vendor", "Satıcı", "text"], ["vendor_address", "Satıcı adresi", "text"],
    ["date", "Belge tarihi", "date"], ["invoice_number", "Fatura numarası", "text"],
    ["due_date", "Vade tarihi", "date"], ["currency", "Para birimi", "currency"],
    ["total_amount", "Toplam tutar", "money"], ["subtotal", "Ara toplam", "money"],
    ["tax_amount", "Vergi", "money"], ["tip_amount", "Bahşiş", "money"],
    ["payment_method", "Ödeme yöntemi", "text"], ["category_suggestion", "Kategori önerisi", "text"],
  ];
  const lineFields = [
    ["description", "Açıklama", "text"], ["quantity", "Miktar", "money"],
    ["unit_price", "Birim fiyat", "money"], ["amount", "Tutar", "money"], ["category", "Kategori", "text"],
  ];
  const readableStatuses = ["extracted", "matched", "pushed"];
  const pushLocks = ["in_progress", "succeeded", "uncertain"];
  const valueText = (value) => value == null ? "" : String(value);
  function workflowLocked() {
    return state.document?.status === "pushed" || pushLocks.includes(state.document?.qbo_push_state)
      || Boolean(state.document?.qbo_transaction_id);
  }
  function writableReceipt() {
    return state.document?.status === "extracted" && ["invoice", "receipt"].includes(state.document?.document_type)
      && Boolean(state.extraction?.receipt_data) && !workflowLocked();
  }
  function canLeaveReview() {
    if (state.reviewBusy) return false;
    return (!state.dirty && !bankChoiceDirty()) || window.confirm("Kaydedilmemiş değişiklikler var. Değişiklikleri bırakıp devam etmek istiyor musun?");
  }
  function reviewControls() {
    const waiting = state.reviewBusy || state.reviewLoading;
    const editable = writableReceipt() && !state.reviewNeedsRefresh;
    el("receipt-fields").disabled = waiting || !editable;
    el("save-review").disabled = waiting || !editable || !state.dirty;
    el("reset-review").disabled = waiting || !state.dirty;
    el("approve-document").disabled = waiting || !editable || state.dirty
      || state.extraction?.approved || !state.extraction?.validation?.is_valid;
    el("refresh-review").disabled = waiting;
    el("extract-document").disabled = waiting || state.reviewNeedsRefresh || workflowLocked()
      || !["uploaded", "failed"].includes(state.document?.status);
    el("close-source").disabled = state.reviewBusy;
    for (const button of document.querySelectorAll("[data-document-open]")) button.disabled = state.reviewBusy;
    el("approval-status").textContent = state.dirty ? "Kaydedilmemiş değişiklikler" : (state.extraction?.approved ? "Onaylı" : "Onay bekliyor");
    qboControls();
    bankControls();
    el("approval-status").className = state.extraction?.approved && !state.dirty ? "badge pushed" : "badge";
  }
  function resetReviewState() {
    resetBank();
    resetQBO();
    state.reviewController?.abort();
    ++state.reviewRequest;
    state.document = null;
    state.extraction = null;
    state.dirty = false;
    state.reviewNeedsRefresh = false;
    state.reviewLoading = false;
    el("review-panel").hidden = true;
    el("review-jump").hidden = true;
  }
  function makeInput(key, title, kind, value, line = false) {
    const wrapper = node("label", undefined, "review-field");
    wrapper.append(node("span", title));
    const input = node("input");
    input.type = kind === "date" ? "date" : "text";
    input.dataset.field = key;
    if (!line) input.id = `receipt-${key}`;
    input.value = valueText(value);
    if (kind === "money") {
      input.inputMode = "decimal";
      input.pattern = "(\\+|-)?([0-9]+(\\.[0-9]*)?|\\.[0-9]+)";
      input.title = "Ondalık ayırıcı olarak nokta kullan (550.00).";
    }
    if (kind === "currency") { input.maxLength = 3; input.pattern = "[A-Za-z]{3}"; input.placeholder = "USD"; }
    if (line && key === "amount") input.required = true;
    wrapper.append(input);
    return wrapper;
  }
  function lineItem(data) {
    const item = node("div", undefined, "line-item");
    item.original = {...data};
    const header = node("div", undefined, "line-item-header");
    header.append(node("strong", "Kalem"));
    const remove = node("button", "Kalemi sil", "button secondary");
    remove.type = "button";
    remove.addEventListener("click", () => { item.remove(); updateDirty(); });
    header.append(remove);
    const fields = node("div", undefined, "line-item-fields");
    for (const [key, title, kind] of lineFields) fields.append(makeInput(key, title, kind, data[key], true));
    item.append(header, fields);
    return item;
  }
  function readInput(input, key) {
    const value = input.value.trim();
    if (key === "description") return value;
    return value === "" ? null : key === "currency" ? value.toUpperCase() : value;
  }
  function reviewPatch() {
    const original = state.extraction?.receipt_data || {};
    const updates = {};
    for (const [key] of receiptFields) {
      const input = el(`receipt-${key}`);
      if (input && input.value !== valueText(original[key])) updates[key] = readInput(input, key);
    }
    const lines = [];
    for (const item of el("line-items").children) {
      const data = {...item.original};
      for (const input of item.querySelectorAll("input")) {
        const key = input.dataset.field;
        if (!Object.hasOwn(data, key) || input.value !== valueText(data[key])) data[key] = readInput(input, key);
      }
      lines.push(data);
    }
    if (JSON.stringify(lines) !== JSON.stringify(original.line_items || [])) updates.line_items = lines;
    return updates;
  }
  function updateDirty() {
    invalidatePlan();
    state.dirty = Object.keys(reviewPatch()).length > 0;
    reviewControls();
  }
  function renderValidation(validation) {
    const panel = el("validation-panel");
    panel.replaceChildren();
    panel.hidden = !validation;
    if (!validation) return;
    panel.classList.toggle("invalid", !validation.is_valid);
    panel.append(node("h3", validation.is_valid ? "Tutar doğrulaması geçti" : "Tutar doğrulamasında hata var"));
    for (const [key, title, className] of [["errors", "Hatalar", ""], ["warnings", "Uyarılar", "validation-warning"]]) {
      if (validation[key]?.length) {
        panel.append(node("strong", title, className));
        const list = node("ul", undefined, className);
        for (const message of validation[key]) list.append(node("li", message));
        panel.append(list);
      }
    }
    panel.append(node("p", `${validation.checks_performed?.length || 0} kontrol yapıldı; ${validation.checks_skipped?.length || 0} kontrol atlandı.`, "muted small"));
  }
  function summaryRows(target, data, fields) {
    const list = node("dl", undefined, "readonly-summary");
    for (const [key, title] of fields) list.append(node("dt", title), node("dd", data[key] == null ? "—" : valueText(data[key])));
    target.append(list);
  }
  function renderBank(data, target) {
    summaryRows(target, data, [["bank_name", "Banka"], ["account_number_last4", "Hesabın son 4 hanesi"],
      ["account_type", "Hesap türü"], ["statement_period_start", "Dönem başlangıcı"], ["statement_period_end", "Dönem sonu"],
      ["beginning_balance", "Başlangıç bakiyesi"], ["ending_balance", "Bitiş bakiyesi"], ["total_deposits", "Toplam giriş"], ["total_withdrawals", "Toplam çıkış"]]);
    target.append(node("h3", `Ekstre işlemleri (${data.transactions.length})`));
    const wrap = node("div", undefined, "extraction-table");
    const table = node("table");
    const head = node("thead"), header = node("tr"), body = node("tbody");
    const columns = [["date", "Tarih"], ["description", "Açıklama"], ["amount", "Tutar"], ["transaction_type", "Tür"],
      ["running_balance", "Bakiye"], ["check_number", "Çek no"], ["vendor_suggestion", "Satıcı önerisi"], ["category_suggestion", "Kategori önerisi"]];
    for (const [, title] of columns) { const cell = node("th", title); cell.scope = "col"; header.append(cell); }
    head.append(header);
    for (const transaction of data.transactions) {
      const row = node("tr");
      for (const [key] of columns) row.append(node("td", transaction[key] == null ? "—" : valueText(transaction[key])));
      body.append(row);
      if (transaction.check_image) {
        const checkRow = node("tr"), cell = node("td"); cell.colSpan = columns.length;
        summaryRows(cell, transaction.check_image, [["check_number", "Çek no"], ["payee", "Alıcı"], ["amount", "Tutar"], ["date", "Tarih"], ["memo", "Açıklama"]]);
        checkRow.append(cell); body.append(checkRow);
      }
    }
    table.append(head, body); wrap.append(table); target.append(wrap);
    target.append(node("h3", `Çıkarılan çekler (${data.check_images.length})`));
    for (const check of data.check_images) {
      const record = node("div", undefined, "check-record");
      summaryRows(record, check, [["check_number", "Çek no"], ["payee", "Alıcı"], ["amount", "Tutar"], ["date", "Tarih"], ["memo", "Açıklama"], ["matched_transaction_id", "Eşleşen QBO işlem ID"]]);
      target.append(record);
    }
    target.append(node("p", "Eşleşme ve kategori kararlarını aşağıdaki ekstre bölümünde incele.", "muted small"));
  }
  function renderReview() {
    const document = state.document, extracted = state.extraction;
    if (!document) return;
    el("review-panel").hidden = false;
    el("review-jump").hidden = false;
    el("review-filename").textContent = `${document.filename} · ${document.id}`;
    el("receipt-form").hidden = true;
    el("readonly-extraction").hidden = true;
    el("raw-extraction").hidden = true;
    el("extract-document").hidden = !["uploaded", "failed", "classifying", "extracting"].includes(document.status);
    renderValidation(extracted?.validation);
    if (state.reviewNeedsRefresh) el("review-message").textContent = "İşlem sonucu doğrulanamadı. Durumu yenile ile sunucudaki kaydı kontrol et.";
    else if (workflowLocked()) el("review-message").textContent = "QBO yazımı başlamış veya tamamlanmış. Bu belge salt okunur.";
    else if (["classifying", "extracting"].includes(document.status)) el("review-message").textContent = "Belge sunucuda işleniyor. Durumu yenileyerek sonucu kontrol edebilirsin.";
    else if (document.status === "failed") el("review-message").textContent = `Veri çıkarma başarısız: ${document.error || "Bilinmeyen hata"}`;
    else if (!extracted) el("review-message").textContent = "Kaynak dosyayı kontrol et, ardından Verileri çıkar düğmesine bas.";
    else el("review-message").textContent = "Çıkarılan verileri kaynak dosyayla karşılaştır.";
    if (extracted?.receipt_data) {
      el("receipt-form").hidden = false;
      el("receipt-inputs").replaceChildren();
      for (const [key, title, kind] of receiptFields) el("receipt-inputs").append(makeInput(key, title, kind, extracted.receipt_data[key]));
      el("line-items").replaceChildren(...extracted.receipt_data.line_items.map(lineItem));
      const confidence = Number(extracted.receipt_data.confidence);
      if (Number.isFinite(confidence)) el("review-message").append(node("span", ` Çıkarma güveni: ${Math.round(confidence * 100)}%.`));
    } else if (extracted) {
      const target = el("readonly-extraction"); target.replaceChildren(); target.hidden = false;
      if (extracted.bank_statement_data) renderBank(extracted.bank_statement_data, target);
      else if (extracted.check_data) summaryRows(target, extracted.check_data, [["check_number", "Çek no"], ["payee", "Alıcı"], ["amount", "Tutar"], ["date", "Tarih"], ["memo", "Açıklama"], ["bank_name", "Banka"], ["account_number_last4", "Hesabın son 4 hanesi"]]);
      else target.append(node("p", "Bu belge türü için çıkarılmış alanlar bulunmuyor.", "muted"));
    }
    const raw = extracted?.receipt_data?.raw_text || extracted?.bank_statement_data?.raw_text;
    if (raw) { el("raw-text").textContent = raw; el("raw-extraction").hidden = false; }
    reviewControls();
  }
  async function loadReview(document, sourceSequence) {
    resetBank();
    resetQBO();
    state.reviewController?.abort();
    const controller = new AbortController(); state.reviewController = controller;
    const requestId = ++state.reviewRequest;
    state.document = document; state.extraction = null; state.dirty = false; state.reviewLoading = true; state.reviewNeedsRefresh = false;
    el("review-error").hidden = true;
    renderReview();
    try {
      if (readableStatuses.includes(document.status)) {
        const data = await json(`${pathFor(document.id)}/extracted`, {signal: controller.signal});
        if (requestId !== state.reviewRequest || sourceSequence !== state.sourceRequest) return;
        state.extraction = data;
        renderReview();
      }
    } catch (error) {
      if (error.name === "AbortError" || requestId !== state.reviewRequest) return;
      state.reviewNeedsRefresh = true;
      el("review-error").textContent = errorMessage(error); el("review-error").hidden = false;
    } finally {
      if (requestId === state.reviewRequest) { state.reviewLoading = false; reviewControls(); if (state.document?.document_type === "bank_statement") void readBank(); }
    }
  }
  async function refreshReview() {
    if (!state.selectedId || !canLeaveReview()) return;
    state.reviewLoading = true; reviewControls();
    const sequence = state.sourceRequest, id = state.selectedId;
    try {
      const document = await json(pathFor(id));
      if (sequence !== state.sourceRequest) return;
      detail(document);
      await loadReview(document, sequence);
    } catch (error) {
      if (sequence !== state.sourceRequest) return;
      state.reviewNeedsRefresh = true;
      el("review-error").textContent = errorMessage(error); el("review-error").hidden = false;
    } finally {
      if (sequence === state.sourceRequest) { state.reviewLoading = false; reviewControls(); }
    }
  }
  async function mutateReview(action) {
    if (state.reviewBusy || state.reviewLoading || state.reviewNeedsRefresh || !state.selectedId) return;
    if (action === "extract" && (workflowLocked() || !["uploaded", "failed"].includes(state.document?.status))) return;
    if (action !== "extract" && !writableReceipt()) return;
    if (action === "approve" && (state.dirty || state.extraction.approved || !state.extraction.validation?.is_valid)) return;
    if (action === "save" && (!state.dirty || !el("receipt-form").reportValidity())) return;
    const id = state.selectedId;
    const endpoint = action === "save" ? "extracted" : action;
    const options = action === "save" ? {method: "PATCH", headers: {"Content-Type": "application/json"}, body: JSON.stringify(reviewPatch())} : {method: "POST"};
    invalidatePlan();
    state.reviewBusy = true;
    el("review-error").hidden = true;
    el("review-message").textContent = action === "extract" ? "Veriler çıkarılıyor… İşlem bitene kadar bekle." : "İşlem kaydediliyor…";
    reviewControls();
    try {
      const data = await json(`${pathFor(id)}/${endpoint}`, options);
      const document = await json(pathFor(id));
      state.document = document;
      state.extraction = readableStatuses.includes(document.status) ? data : null;
      state.dirty = false;
      detail(document); renderReview();
      if (document.status !== "failed") el("review-message").textContent = action === "approve" ? "Belge onaylandı." : action === "save" ? (data.validation?.is_valid ? "Değişiklikler kaydedildi. Belgeyi yeniden onayla." : "Değişiklikler kaydedildi. Doğrulama hatalarını düzelt.") : "Veri çıkarma tamamlandı. Alanları kaynakla karşılaştır.";
      await loadDocuments();
      if (document.document_type === "bank_statement") void readBank();
    } catch (error) {
      state.reviewNeedsRefresh = !(error instanceof APIError) || error.status === 409 || error.status >= 500;
      el("review-message").textContent = "İşlem tamamlanamadı. Hata ayrıntısını kontrol et.";
      el("review-error").textContent = error instanceof APIError ? errorMessage(error) : "İşlemin sonucu doğrulanamadı. İşlemi tekrar göndermeden önce Durumu yenile ile kontrol et.";
      el("review-error").hidden = false;
      // Reading is safe after a failed mutation; never automatically repeat POST/PATCH.
      try { state.document = await json(pathFor(id)); detail(state.document); } catch (_) { /* Keep the existing draft. */ }
    } finally {
      state.reviewBusy = false; reviewControls();
    }
  }

  const bank = {data: null, realm: null, accounts: [], ready: false, loading: false, request: 0, plan: null, uncertain: false};
  function resetBank() {
    ++bank.request; bank.data = null; bank.realm = null; bank.accounts = []; bank.ready = false; bank.loading = false; bank.plan = null; bank.uncertain = false;
    el("bank-plan").hidden = true; el("bank-error").hidden = true; el("bank-result").textContent = "";
    fillOptions("bank-account", [], "Banka hesabı seç");
    for (const id of ["bank-matches", "bank-checks", "bank-category-records"]) el(id).replaceChildren();
  }
  function bankEditable() {
    return state.document?.document_type === "bank_statement" && ["extracted", "matched"].includes(state.document.status)
      && bank.data && !bank.data.locked && !state.reviewNeedsRefresh && !bank.uncertain;
  }
  function bankChoiceDirty() { return Boolean(el("bank-panel").querySelector('[data-bank-pending="true"]')); }
  function bankControls() {
    el("bank-panel").hidden = state.document?.document_type !== "bank_statement";
    const waiting = state.reviewBusy || state.reviewLoading || bank.loading, editable = bankEditable();
    const mappingChanged = bank.ready && (bank.realm !== bank.data?.realm_id || el("bank-account").value !== bank.data?.bank_account?.id);
    const choicesPending = bankChoiceDirty();
    const matchesDone = bank.data?.matches?.length > 0 && bank.data.matches.every((item) => ["approved", "rejected", "manual_override"].includes(item.review_status));
    const vendorsDone = bank.data?.checks?.every((item) => ["matched", "no_vendor_name", "skipped_rejected"].includes(item.vendor_status));
    el("bank-refresh").disabled = waiting;
    el("bank-load").disabled = waiting || !editable || choicesPending;
    el("bank-account").disabled = waiting || !editable || !bank.ready;
    el("bank-match").disabled = waiting || !editable || !bank.ready || !el("bank-account").value;
    el("bank-vendors").disabled = waiting || !editable || !matchesDone || mappingChanged || choicesPending;
    el("bank-categories").disabled = waiting || !editable || !matchesDone || !vendorsDone || mappingChanged || choicesPending;
    el("bank-preview").disabled = waiting || !editable || !bank.data?.push_ready || mappingChanged || choicesPending;
    el("bank-push").disabled = waiting || !editable || !bank.plan || bank.plan.revision !== bank.data?.revision || mappingChanged || choicesPending;
    for (const button of el("bank-panel").querySelectorAll("[data-bank-mutation]")) button.disabled = waiting || !editable || mappingChanged || button.dataset.unavailable === "true";
    for (const select of el("bank-panel").querySelectorAll("[data-bank-choice]")) select.disabled = waiting || !editable || mappingChanged;
    if (!waiting) el("bank-message").textContent = bank.uncertain || state.reviewNeedsRefresh ? "İşlem sonucu doğrulanamadı. Ekstre durumunu yenile; QBO yazımını tekrar gönderme."
      : bank.data?.locked ? "Yazım başlamış veya tamamlanmış. Kayıtlar salt okunur; aşağıdaki QBO işlem ve ek durumlarını kontrol et."
      : mappingChanged ? "Banka hesabı veya QBO şirketi seçimi değişti. QBO ile eşleştir düğmesiyle eşleşmeyi yenile."
      : choicesPending ? "Seçilen adayı/hesabı kaydetmeden plan oluşturulamaz." : "Banka hesabını seç, eşleşmeleri incele, satıcıları kontrol et ve kategori kararlarını kaydet.";
  }
  function invalidateBankPlan() { bank.plan = null; el("bank-plan").hidden = true; }
  const reviewLabels = {pending: "İnceleme bekliyor", approved: "Onaylandı", rejected: "Reddedildi", manual_override: "Elle seçildi"};
  function bankButton(title, action, options, unavailable = false) {
    const button = node("button", title, "button secondary"); button.type = "button"; button.dataset.bankMutation = "true";
    button.dataset.unavailable = String(unavailable); button.addEventListener("click", () => bankAction(action, typeof options === "function" ? options() : options)); return button;
  }
  function candidateText(candidate) {
    const txn = candidate.transaction || {};
    return `${txn.type || ""} · ${txn.id || ""} · ${txn.date || ""} · ${txn.amount ?? ""} · ${txn.description || ""} · puan ${candidate.score ?? "—"}`;
  }
  function renderBankWorkflow() {
    const data = bank.data; if (!data) return;
    el("bank-binding").textContent = data.realm_id ? `Eşleşmenin şirket ID’si: ${data.realm_id} · Banka: ${data.bank_account?.name || "—"} (${data.bank_account?.id || "—"})`
      : "Henüz şirket/banka bilgisiyle eşleşme kaydedilmedi. QBO hesaplarını yükleyip eşleştir.";
    const matchTarget = el("bank-matches"); matchTarget.replaceChildren();
    if (data.matches === null) matchTarget.append(node("p", "Henüz QBO eşleşmesi yok.", "muted"));
    for (const item of data.matches || []) {
      const txn = state.extraction?.bank_statement_data?.transactions[item.transaction_index] || {};
      const card = node("div", undefined, "bank-record"); card.dataset.matchIndex = String(item.transaction_index);
      card.append(node("h5", `İşlem ${item.transaction_index + 1} · ${txn.date || ""} · ${txn.amount ?? ""}`), node("p", txn.description || ""), node("p", label(reviewLabels, item.review_status)));
      card.append(node("p", item.best_match ? `Öneri: ${candidateText(item.best_match)}` : "Uygun QBO önerisi bulunamadı."));
      if (item.reviewed_match) card.append(node("p", `Kaydedilen seçim: ${candidateText(item.reviewed_match)}`));
      const select = node("select"); select.dataset.bankChoice = "true"; select.setAttribute("aria-label", `İşlem ${item.transaction_index + 1} QBO adayı`);
      const empty = node("option", "QBO adayı seç"); empty.value = ""; select.append(empty);
      for (const candidate of item.candidates || []) {
        const option = node("option", candidateText(candidate)); option.value = JSON.stringify({qbo_transaction_id: String(candidate.transaction.id), qbo_transaction_type: candidate.transaction.type}); select.append(option);
      }
      const actions = node("div", undefined, "review-actions");
      actions.append(bankButton("Öneriyi onayla", "match-review", {index: item.transaction_index, body: {decision: "approve"}}, !item.matched || !item.best_match),
        bankButton("Eşleşmeyi reddet", "match-review", {index: item.transaction_index, body: {decision: "reject"}}));
      const manual = bankButton("Seçilen adayı kaydet", "match-review", () => ({index: item.transaction_index, body: {decision: "manual_override", ...JSON.parse(select.value || "{}")}}), true);
      select.addEventListener("change", () => { select.dataset.bankPending = String(Boolean(select.value)); invalidateBankPlan(); manual.dataset.unavailable = String(!select.value); bankControls(); });
      actions.append(manual); card.append(select, actions); matchTarget.append(card);
    }
    const checksTarget = el("bank-checks"); checksTarget.replaceChildren(node("h4", `Çek/satıcı kontrolü (${data.checks.length})`));
    if (!data.checks.length) checksTarget.append(node("p", "Çek görseli yok; çek eki yüklenmeyecek.", "muted"));
    for (const item of data.checks) {
      const card = node("div", undefined, "bank-record");
      const extracted = state.extraction?.bank_statement_data?.check_images[item.check_index] || {};
      card.append(node("h5", `Çek ${extracted.check_number || item.check_index + 1} · ${item.vendor_name || "Satıcı adı yok"}`));
      summaryRows(card, {...item, amount: extracted.amount}, [["amount", "Çek tutarı"], ["vendor_status", "Satıcı durumu"], ["qbo_transaction_id", "QBO işlem ID"], ["qbo_transaction_type", "QBO işlem türü"], ["attachment_push_status", "Ek durumu"], ["attachment_id", "Ek ID"], ["attachment_error", "Ek hatası"]]);
      if (item.vendor_status === "missing" && item.vendor_name) card.append(bankButton("Eksik satıcıyı QBO’da oluştur", "create-vendor", {name: item.vendor_name}));
      checksTarget.append(card);
    }
    const categories = el("bank-category-records"); categories.replaceChildren();
    if (data.categories === null) categories.append(node("p", "Kategori önerileri henüz oluşturulmadı.", "muted"));
    else if (!data.categories.length) categories.append(node("p", "İncelenecek Purchase kategorisi yok.", "muted"));
    for (const item of data.categories || []) {
      const card = node("div", undefined, "bank-record"); card.dataset.categoryIndex = String(item.transaction_index);
      card.append(node("h5", `İşlem ${item.transaction_index + 1} · QBO ${item.qbo_transaction_type} ${item.qbo_transaction_id}`), node("p", item.description || ""), node("p", label(reviewLabels, item.review_status)));
      summaryRows(card, {...item, selected: item.reviewed_category?.name}, [["category_suggestion", "Önerilen hesap"], ["selected", "Kaydedilen hesap"], ["qbo_category_push_status", "Kategori yazım durumu"], ["qbo_category_push_error", "Yazım hatası"]]);
      const select = node("select"); select.dataset.bankChoice = "true"; select.setAttribute("aria-label", `İşlem ${item.transaction_index + 1} gider hesabı`);
      const empty = node("option", "Gider hesabı seç"); empty.value = ""; select.append(empty);
      for (const account of bank.accounts) { const option = node("option", `${account.name} · ${account.id}`); option.value = String(account.id); select.append(option); }
      const actions = node("div", undefined, "review-actions");
      actions.append(bankButton("Kategoriyi onayla", "category-review", {index: item.transaction_index, body: {decision: "approve"}}, !item.category_match),
        bankButton("Kategori yazımını reddet", "category-review", {index: item.transaction_index, body: {decision: "reject"}}));
      const manual = bankButton("Seçilen hesabı kaydet", "category-review", () => ({index: item.transaction_index, body: {decision: "manual_override", qbo_account_id: select.value}}), true);
      select.addEventListener("change", () => { select.dataset.bankPending = String(Boolean(select.value)); invalidateBankPlan(); manual.dataset.unavailable = String(!select.value); bankControls(); });
      actions.append(manual); card.append(select, actions); categories.append(card);
    }
    bankControls();
  }
  async function readBank(clearError = false) {
    if (state.document?.document_type !== "bank_statement") return;
    const id = state.selectedId, source = state.sourceRequest, requestId = ++bank.request;
    bank.loading = true; invalidateBankPlan(); if (clearError) el("bank-error").hidden = true; bankControls();
    try {
      const data = await json(`${pathFor(id)}/bank-workflow`);
      if (source !== state.sourceRequest || requestId !== bank.request) return;
      bank.data = data; bank.uncertain = false; state.reviewNeedsRefresh = false;
      state.document.status = data.status; detail(state.document); renderBankWorkflow();
    } catch (error) {
      if (source !== state.sourceRequest || requestId !== bank.request) return;
      state.reviewNeedsRefresh = true; el("bank-error").textContent = errorMessage(error); el("bank-error").hidden = false;
    } finally { if (requestId === bank.request) { bank.loading = false; reviewControls(); } }
  }
  async function bankAction(action, options = {}) {
    if (state.reviewBusy || state.reviewLoading || bank.loading || !bankEditable()) return;
    const mainButton = {load: "bank-load", match: "bank-match", vendors: "bank-vendors", categories: "bank-categories", preview: "bank-preview", push: "bank-push"}[action];
    if (mainButton && el(mainButton).disabled) return;
    if (action === "match" && bank.data.matches !== null && !window.confirm("Yeniden eşleşme mevcut eşleşme, satıcı ve kategori kararlarını sıfırlar. Devam edilsin mi?")) return;
    if (action === "categories" && bank.data.categories !== null && !window.confirm("Kategori önerilerini yeniden oluşturmak kategori kararlarını sıfırlar. Devam edilsin mi?")) return;
    if (action === "create-vendor" && !window.confirm(`${options.name} satıcısını QuickBooks’ta oluşturmak istiyor musun?`)) return;
    if (action === "push" && !window.confirm("Önizlemedeki mevcut QBO işlemlerinin kategorilerini güncellemek ve çek eklerini yüklemek istiyor musun?")) return;
    const id = state.selectedId, plan = bank.plan;
    state.reviewBusy = true; el("bank-error").hidden = true; el("bank-result").textContent = ""; reviewControls();
    el("bank-message").textContent = action === "push" ? "QBO aktarımı sürüyor… Tekrar gönderme." : "Ekstre işlemi yürütülüyor…";
    try {
      if (action === "load") {
        bank.ready = false; invalidateBankPlan();
        const status = await json("/api/qbo/status"); if (!status.connected || !status.company_id) throw new APIError("Önce QBO bağlantısını kur.", 400);
        const data = await json("/api/qbo/accounts"), after = await json("/api/qbo/status");
        if (!after.connected || after.company_id !== status.company_id) throw new APIError("QBO şirketi değişti. Hesapları tekrar yükle.", 409);
        bank.realm = status.company_id; bank.accounts = data.accounts.filter((item) => item.type === "Expense");
        fillOptions("bank-account", data.accounts.filter((item) => item.type === "Bank"), "Banka hesabı seç");
        if (bank.data.realm_id === bank.realm && bank.data.bank_account?.id) el("bank-account").value = bank.data.bank_account.id;
        bank.ready = true; renderBankWorkflow();
      } else if (action === "preview") {
        await readBank();
        if (!bank.data?.push_ready || state.reviewNeedsRefresh) throw new APIError("Ekstre hazır değil. İnceleme kararlarını ve satıcı durumunu kontrol et.", 400);
        bank.plan = {revision: bank.data.revision, realm: bank.data.realm_id};
        const target = el("bank-plan"); target.replaceChildren(node("h4", "Ekstre aktarım planı")); target.hidden = false;
        target.append(node("p", `Şirket: ${bank.data.realm_id} · Banka: ${bank.data.bank_account?.name || "—"}. Yeni Bill/Expense oluşturulmaz.`));
        for (const item of bank.data.plan.category_updates) target.append(node("p", `İşlem ${item.transaction_index + 1}: Purchase ${item.qbo_transaction_id} → ${item.category_name || item.account_id} (${item.account_id})`));
        for (const item of bank.data.plan.check_attachments) target.append(node("p", `Çek ${item.check_index + 1}: ${item.qbo_transaction_type} ${item.qbo_transaction_id} kaydına görsel eki.`));
        target.append(node("p", `${bank.data.plan.category_updates.length} kategori güncellemesi; ${bank.data.plan.check_attachments.length} çek eki. Reddedilen kayıtlar aktarılmaz.`));
      } else if (action === "create-vendor") {
        invalidateBankPlan();
        await json("/api/qbo/vendors", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({display_name: options.name, expected_realm_id: bank.data.realm_id})});
        await json(`${pathFor(id)}/resolve-vendors`, {method: "POST"}); await readBank();
      } else {
        const suffix = {match: "match-to-qbo", vendors: "resolve-vendors", categories: "suggest-categories", push: "push-to-qbo"}[action] || `${action}/${options.index}`;
        const body = action === "match" ? {bank_account_id: el("bank-account").value, expected_realm_id: bank.realm}
          : action === "push" ? {attach_documents: true, expected_realm_id: plan.realm, expected_revision: plan.revision} : options.body;
        invalidateBankPlan();
        const response = await json(`${pathFor(id)}/${suffix}`, {method: action.endsWith("review") ? "PATCH" : "POST", ...(body ? {headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)} : {})});
        await readBank(); await loadDocuments();
        if (action === "push") {
          el("bank-result").textContent = `${response.categories_updated} kategori güncellendi; ${response.attachments_uploaded} çek eki yüklendi; ${response.categories_skipped} kategori atlandı.`;
          if (response.errors?.length) { el("bank-error").textContent = response.errors.join("; "); el("bank-error").hidden = false; }
        }
      }
    } catch (error) {
      invalidateBankPlan(); el("bank-error").textContent = errorMessage(error); el("bank-error").hidden = false;
      if (action === "load") bank.ready = false;
      const uncertain = !(error instanceof APIError) || error.status === 409 || error.status >= 500;
      if (uncertain) { bank.uncertain = true; state.reviewNeedsRefresh = true; }
      // Read persisted checkpoints after a failed write. Never repeat POST/PATCH automatically.
      if (action === "push") { await readBank(); await loadDocuments(); }
    } finally { state.reviewBusy = false; reviewControls(); }
  }
  el("bank-refresh").addEventListener("click", () => { if (canLeaveReview()) void readBank(true); });
  el("bank-account").addEventListener("input", () => { invalidateBankPlan(); bankControls(); });
  for (const action of ["load", "match", "vendors", "categories", "preview", "push"]) el(`bank-${action}`).addEventListener("click", () => bankAction(action));

  // Mapping and preview live only for the currently selected, approved document.
  const qbo = {realm: null, ready: false, plan: null, uncertain: false};
  function invalidatePlan() { qbo.plan = null; el("qbo-plan").hidden = true; }
  function resetQBO() {
    qbo.realm = null; qbo.ready = false; qbo.uncertain = false; invalidatePlan();
    el("qbo-error").hidden = true;
    for (const [id, title] of [["qbo-vendor", "Satıcı seç"], ["qbo-account", "Gider hesabı seç"], ["qbo-bank", "Banka hesabı seç"]]) fillOptions(id, [], title);
    el("qbo-description").value = ""; el("qbo-suggestions").textContent = "";
    el("qbo-create-as").value = "bill";
  }
  function qboEligible() {
    return writableReceipt() && state.extraction?.approved && state.extraction?.validation?.is_valid
      && !state.dirty && !state.reviewNeedsRefresh && !qbo.uncertain;
  }
  function qboPayload() {
    return {create_as: el("qbo-create-as").value, vendor_id: el("qbo-vendor").value,
      account_id: el("qbo-account").value, bank_account_id: el("qbo-create-as").value === "expense" ? el("qbo-bank").value : null,
      description: el("qbo-description").value.trim() || null, expected_realm_id: qbo.realm};
  }
  function qboControls() {
    el("qbo-panel").hidden = !["invoice", "receipt"].includes(state.document?.document_type);
    const waiting = state.reviewBusy || state.reviewLoading, allowed = qboEligible();
    el("qbo-load").disabled = waiting || !allowed;
    el("qbo-fields").disabled = waiting || !allowed || !qbo.ready;
    el("qbo-bank-label").hidden = el("qbo-create-as").value !== "expense";
    const body = qboPayload();
    el("qbo-preview").disabled = waiting || !allowed || !qbo.ready || !body.vendor_id || !body.account_id
      || (body.create_as === "expense" && !body.bank_account_id);
    el("qbo-push").disabled = waiting || !allowed || !qbo.plan || JSON.stringify(body) !== qbo.plan.signature;
    const doc = state.document;
    el("qbo-result").replaceChildren(); el("qbo-result").hidden = !workflowLocked();
    if (workflowLocked()) {
      for (const [title, value] of [["Gönderim durumu", doc.qbo_push_state], ["QBO işlem türü", doc.qbo_transaction_type],
        ["QBO işlem ID", doc.qbo_transaction_id], ["Ek ID", doc.qbo_attachment_id], ["Gönderim hatası", doc.qbo_push_error], ["Ek hatası", doc.qbo_attachment_error]]) {
        if (value) el("qbo-result").append(node("dt", title), node("dd", value));
      }
      el("qbo-message").textContent = doc.qbo_attachment_error ? "QBO işlemi oluşturuldu; kaynak eki tamamlanamadı. Yukarıdaki işlem ID’sini QuickBooks’ta kontrol et. Yeniden işlem oluşturma."
        : ["uncertain", "in_progress"].includes(doc.qbo_push_state) ? "Gönderim sonucu belirsiz veya işlem sürüyor. QuickBooks’taki kaydı kontrol et; tekrar gönderim kapalı."
        : "QBO işlem kaydı korunuyor. Bu belge tekrar gönderilemez.";
    } else if (!waiting) el("qbo-message").textContent = qbo.uncertain || state.reviewNeedsRefresh ? "Sonuç doğrulanamadı. Önce Durumu yenile ile sunucudaki kaydı kontrol et."
      : !allowed ? "Belgeyi kaydet, doğrulama hatalarını düzelt ve onayla."
      : qbo.ready ? `Bağlı QBO şirketi: ${qbo.realm}. Seçimlerini kontrol edip planı önizle. Kaynak dosya gönderime eklenecek.` : "QBO seçeneklerini yükle; satıcı ve hesaplarını seç.";
  }
  function fillOptions(id, items, placeholder) {
    const select = el(id); select.replaceChildren(node("option", placeholder)); select.firstChild.value = "";
    for (const item of items) {
      if (item.active === false) continue;
      const option = node("option", `${item.fully_qualified_name || item.name} · ${item.id}${item.currency ? ` · ${item.currency}` : ""}`);
      option.value = String(item.id); select.append(option);
    }
  }
  async function qboAction(action) {
    if (state.reviewBusy || state.reviewLoading || !qboEligible()) return;
    if (action === "preview" && el("qbo-preview").disabled || action === "push" && el("qbo-push").disabled) return;
    const id = state.selectedId, body = qboPayload();
    if (action === "push" && !window.confirm("Önizlediğin işlemi QuickBooks’ta oluşturup kaynak dosyayı eklemek istiyor musun?")) return;
    state.reviewBusy = true; el("qbo-error").hidden = true; reviewControls();
    el("qbo-message").textContent = action === "load" ? "QBO seçenekleri okunuyor…" : action === "preview" ? "Plan doğrulanıyor…" : "QBO’ya gönderiliyor… Sonuç gelene kadar bekle.";
    try {
      if (action === "load") {
        qbo.ready = false; invalidatePlan();
        const status = await json("/api/qbo/status");
        if (!status.connected || !status.company_id) throw new APIError("Önce QBO bağlantısını kur.", 400);
        const [options, vendors, accounts] = await Promise.all([
          json(`/api/qbo/invoice-options/${encodeURIComponent(id)}`), json("/api/qbo/vendors"), json("/api/qbo/accounts")]);
        const after = await json("/api/qbo/status");
        if (!after.connected || after.company_id !== status.company_id) throw new APIError("QBO şirketi değişti. Seçenekleri tekrar yükle.", 409);
        qbo.realm = status.company_id;
        fillOptions("qbo-vendor", vendors.vendors, "Satıcı seç");
        fillOptions("qbo-account", options.expense_accounts, "Gider hesabı seç");
        fillOptions("qbo-bank", accounts.accounts.filter((item) => item.type === "Bank"), "Banka hesabı seç");
        el("qbo-create-as").value = state.document.document_type === "receipt" ? "expense" : "bill";
        el("qbo-suggestions").textContent = `Satıcı önerisi: ${options.vendor_match?.name || "eşleşme bulunamadı"}. Gider hesabı önerileri: ${options.category_account_matches.map((item) => item.name).join(", ") || "bulunamadı"}. Seçimleri kendin yap.`;
        qbo.ready = true;
      } else if (action === "preview") {
        invalidatePlan();
        const data = await json(`/api/qbo/invoice-plan/${encodeURIComponent(id)}`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
        if (data.document_id !== id || data.realm_id !== qbo.realm) throw new APIError("Planın belge veya QBO şirketi doğrulanamadı.", 409);
        qbo.plan = {signature: JSON.stringify(body), body};
        const target = el("qbo-plan"); target.replaceChildren(node("h3", "Gönderim planı")); target.hidden = false;
        summaryRows(target, {company: qbo.realm, kind: data.create_as, vendor: data.vendor.name, account: data.account.name,
          bank: body.bank_account_id ? el("qbo-bank").selectedOptions[0].textContent : "—", currency: data.currency_check.document_currency,
          home: data.currency_check.home_currency, attachment: "Kaynak dosya eklenecek"}, [["company", "QBO şirket ID"], ["kind", "İşlem türü"], ["vendor", "Satıcı"], ["account", "Gider hesabı"], ["bank", "Banka hesabı"], ["currency", "Belge para birimi"], ["home", "QBO ana para birimi"], ["attachment", "Ek"]]);
        summaryRows(target, data.plan.kwargs, [["amount", "Toplam tutar"], ["date", "İşlem tarihi"], ["due_date", "Vade tarihi"], ["description", "Açıklama"]]);
        target.append(node("p", "Belge toplamı seçilen gider hesabına tek işlem olarak yazılır.", "muted small"));
      } else {
        const response = await json(`${pathFor(id)}/push-to-qbo`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({...qbo.plan.body, attach_documents: true})});
        invalidatePlan();
        const doc = await json(pathFor(id)); state.document = doc; detail(doc);
        if (response.errors?.length) { el("qbo-error").textContent = response.errors.join("; "); el("qbo-error").hidden = false; }
        await loadDocuments();
      }
    } catch (error) {
      invalidatePlan(); el("qbo-error").textContent = errorMessage(error); el("qbo-error").hidden = false;
      if (action === "load" || error.status === 409) qbo.ready = false;
      if (action === "push") {
        qbo.uncertain = true; state.reviewNeedsRefresh = true;
        // Never repeat an external write after a failed or lost response.
        try { state.document = await json(pathFor(id)); detail(state.document); await loadDocuments(); } catch (_) { /* Explicit refresh is required. */ }
      }
    } finally { state.reviewBusy = false; reviewControls(); }
  }
  for (const id of ["qbo-create-as", "qbo-vendor", "qbo-account", "qbo-bank", "qbo-description"]) {
    el(id).addEventListener("input", () => { invalidatePlan(); qboControls(); });
  }
  for (const action of ["load", "preview", "push"]) el(`qbo-${action}`).addEventListener("click", () => qboAction(action));

  function uploadControls(busy) {
    state.uploadBusy = busy;
    for (const id of ["file-input", "choose-files", "upload-zone"]) el(id).disabled = busy;
    el("upload-zone").classList.remove("dragover");
  }
  async function uploadFiles(files) {
    if (state.uploadBusy || files.length === 0) return;
    uploadControls(true);
    el("upload-results").replaceChildren();
    el("upload-results").hidden = false;
    let succeeded = 0;
    let failed = 0;
    for (let index = 0; index < files.length; index++) {
      const file = files[index];
      el("upload-status").textContent = `Yükleniyor (${index + 1}/${files.length}): ${file.name}`;
      const result = node("li", `${file.name} — Yükleniyor…`, "upload-result");
      el("upload-results").append(result);
      try {
        const extension = file.name.split(".").pop().toLowerCase();
        if (!Object.hasOwn(mediaByExtension, extension)) throw new APIError("Desteklenmeyen dosya. PDF, PNG, JPG veya HEIC seç.");
        if (file.size === 0) throw new APIError("Dosya boş; başka bir dosya seç.");
        // Some Windows/browser combinations send an empty MIME for HEIC.
        const normalized = new File([file], file.name, {type: mediaByExtension[extension], lastModified: file.lastModified});
        const form = new FormData();
        form.append("file", normalized);
        const data = await json("/api/documents/upload?company_id=default&auto_process=false", {method: "POST", body: form});
        if (!data.id) throw new Error("Upload response has no document ID");
        succeeded++;
        result.classList.add("success");
        result.textContent = `${file.name} — Kaydedildi`;
        const open = node("button", "Kaynağı aç", "button secondary");
        open.type = "button";
        open.dataset.documentOpen = "true";
        open.disabled = state.reviewBusy;
        open.addEventListener("click", () => openSource(data.id));
        result.append(open);
      } catch (error) {
        failed++;
        result.classList.add("error");
        result.textContent = `${file.name} — ${error instanceof APIError ? errorMessage(error) : "Yükleme sonucu doğrulanamadı. Tekrar yüklemeden önce listeyi yenileyerek kontrol et."}`;
      }
    }
    el("upload-status").textContent = `${succeeded} dosya kaydedildi${failed ? `; ${failed} dosya için yükleme tamamlanamadı` : ""}.`;
    el("file-input").value = "";
    uploadControls(false);
    await loadDocuments(0);
  }
  async function checkQBO() {
    try {
      const data = await json("/api/qbo/status");
      el("qbo-status").classList.toggle("connected", data.connected);
      el("qbo-status").textContent = data.connected ? `QBO · ${data.company_name || data.company_id}` : (data.reconnect_required ? "QBO · Yeniden bağlantı gerekli" : "QBO · Bağlı değil");
      el("qbo-connect").textContent = data.reconnect_required ? "Yeniden bağlan" : "QBO’ya bağlan";
      el("qbo-connect").hidden = data.connected;
    } catch (error) {
      el("qbo-status").textContent = "QBO · Bağlantı durumu okunamadı";
      el("qbo-status").title = errorMessage(error);
      el("qbo-connect").hidden = false;
    }
  }
  el("choose-files").addEventListener("click", () => el("file-input").click());
  el("upload-zone").addEventListener("click", () => el("file-input").click());
  el("file-input").addEventListener("change", (event) => uploadFiles(Array.from(event.target.files)));
  for (const eventName of ["dragenter", "dragover", "dragleave", "drop"]) {
    el("upload-zone").addEventListener(eventName, (event) => {
      event.preventDefault();
      if (!state.uploadBusy) el("upload-zone").classList.toggle("dragover", ["dragenter", "dragover"].includes(eventName));
    });
  }
  el("upload-zone").addEventListener("drop", (event) => uploadFiles(Array.from(event.dataTransfer.files)));
  el("refresh-list").addEventListener("click", () => { loadDocuments(); checkQBO(); });
  el("previous-page").addEventListener("click", () => loadDocuments(Math.max(0, state.offset - state.limit)));
  el("next-page").addEventListener("click", () => loadDocuments(state.offset + state.limit));
  el("close-source").addEventListener("click", closeSource);
  window.addEventListener("pagehide", () => { if (state.sourceUrl) URL.revokeObjectURL(state.sourceUrl); });
  el("receipt-form").addEventListener("input", updateDirty);
  el("receipt-form").addEventListener("submit", (event) => { event.preventDefault(); mutateReview("save"); });
  el("extract-document").addEventListener("click", () => mutateReview("extract"));
  el("approve-document").addEventListener("click", () => mutateReview("approve"));
  el("refresh-review").addEventListener("click", refreshReview);
  el("reset-review").addEventListener("click", () => { state.dirty = false; if (!state.reviewNeedsRefresh) el("review-error").hidden = true; renderReview(); });
  el("add-line-item").addEventListener("click", () => { el("line-items").append(lineItem({description: "", quantity: 1, unit_price: null, amount: null, category: null})); updateDirty(); });
  window.addEventListener("beforeunload", (event) => {
    if (state.dirty || state.reviewBusy || bankChoiceDirty()) { event.preventDefault(); event.returnValue = ""; }
  });
  loadDocuments(0);
  checkQBO();
})();
