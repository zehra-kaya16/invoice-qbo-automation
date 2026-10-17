# Adım 110 — Bağımsız çek ve kredi kartı ekstresi

Bu paket `invoice-qbo-automation-main (4)(1).zip` üzerine hazırlandı. Karşılaştırılan referans `receipt-ai-main(20261016-044701).zip` dosyasıdır; GitHub main'in daha sonraki sürümlerinin denetlendiği anlamına gelmez.

## 1. Dosyaları uygula

Sunucuyu durdur. ZIP'i ayrı bir klasöre aç. İçindeki `app`, `tests` ve `test-data` klasörlerini projenin köküne aynı yolları koruyarak kopyala; `requirements.txt` dosyasını da kökte değiştir.

| Dosya | Uygulanan değişiklik |
|---|---|
| `app/schemas/documents.py` | Ortak inceleme türleri, kullanıcının seçtiği çek yönü, kredi kartı ekstresi cevabı. |
| `app/services/extraction/reconciliation.py` | Yeni: kredi kartının tüm sayfalarını çıkarma; çek kanıtını mevcut eşleşme akışına uyarlama; doğrulama. |
| `app/services/extraction/extractor.py` | Çekin ön/arka sayfalarını birlikte okuma, birden fazla ayrı çek için hata, çek numarasında baştaki sıfırları koruma. |
| `app/api/documents.py` | Yeni türler için extraction, alan düzeltme, eşleşme, satıcı/kategori incelemesi ve aktarım; kaynak ekinin doğru MIME türü. |
| `app/services/matching/matcher.py` | Alınan çekin pozitif hesap hareketine bağlanabilmesi. |
| `app/services/qbo/client.py` | CreditCard hesabı adayları; harcama/iade, Transfer, kart borcu ödemesi ve kredi kartıyla BillPayment okuma. |
| `app/services/persistence/check_sources.py` | Bağımsız çekin PDF/görsel kaynak baytları ve MIME türüyle çalışması. |
| `app/api/recovery.py` | Yeni türlerin kalıcı toparlanma akışına katılması; çek numarası ve incelenen tahsil/ödeme tarihinin doğrulanması. |
| `app/services/persistence/batch_push.py` | Yeni türler için incelenmiş plan kaydı, toplu aktarım ve sonuç okuma. |
| `app/api/export.py` | Kredi kartı ekstresinin JSON çıktısında işlem satırlarının korunması. |
| `app/static/app.js` | Çek yönü ve alan düzeltme; kredi kartı dönem/işlem düzeltme; hesap seçimi, inceleme, önizleme ve aktarım. |
| `app/templates/index.html` | Düzeltme alanı ve belge türüne uygun hesap başlığı. |
| `requirements.txt` | Test edilen SDK: `python-quickbooks>=0.9.12,<0.10`; kart ödeme entity'si kullanılıyor. |
| `tests/test_reconciliation_types.py` | Yeni: 30 test. |

Proje klasöründe PowerShell:

```powershell
python -m pip install -r requirements.txt
python -m pytest -q tests/test_reconciliation_types.py
python -m pytest -q
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Tarayıcıda Ctrl+F5 ile arayüzü yenile.

## 2. Bağımsız çek — gerçek sandbox kontrolü

1. `test-data/adim110_check.png` dosyasını yükle; **Verileri çıkar** düğmesine bas.
2. **Çıkarılan alanları düzelt** bölümünü aç. Çek no `00200`, tarih `2026-09-15`, tutar `550.00`, alıcı `Books by Bessie` alanlarını kaynakla karşılaştır.
3. **Çek yönü** seç: bu örnekte **Verilen / ödenen çek**. **Düzeltilen alanları kaydet** düğmesine bas. Yön AI tarafından varsayılmıyor.
4. QBO sandbox'ta seçilecek Bank hesabında aynı çek numarası ve tutarı taşıyan mevcut Purchase/Check veya BillPayment kaydı olmalı. Gerekirse bu test kaydını sandbox'ta bir kez hazırla. Uygulama bu aşamada yeni finansal kayıt oluşturmuyor.
5. **QBO hesaplarını yükle** → Bank hesabını seç → **QBO ile eşleştir**.
6. Önerilen kaydın numarasını, tutarını ve tarihini kontrol et. **Öneriyi onayla** veya listeden doğru adayı seçip kaydet. Yanlış eşleşmeyi reddet.
7. **Satıcıları kontrol et**. Verilen çekin alıcısı için satıcı kontrolünü tamamla.
8. **Kategori önerilerini oluştur**. Purchase için doğru gider hesabını inceleyip onayla/seç; kategori yazılmasını istemediğin satırı reddet. BillPayment gibi Purchase olmayan kayıtlarda gider kategorisi yazılmaz.
9. **Ekstre planını önizle** → **Onaylanan kategorileri ve çek eklerini aktar**.
10. Mevcut QBO işleminde kaynak eki bulunduğunu doğrula. Yeni Bill/Expense oluşmamalı. Aynı belgeyi tekrar gönderdiğinde yeni ek veya tekrarlanan kategori yazımı oluşmamalı.
11. Sunucuyu kapatıp yeniden aç. Aynı belge ID'sinde sonuç ve ek ID'si görünmeli; kaynak dosya açılabilmeli.

Alınan çekte **Alınan / tahsil edilen çek** seçilir; pozitif hesap hareketi taşıyan mevcut Deposit veya Customer Payment ile eşleşir. Bu yolda Vendor oluşturma ve Purchase gider kategorisi yazma yapılmaz. Çek numarası PaymentRefNum/DocNumber üzerinden eşleşmelidir. QBO kaydında numara eksik veya farklıysa önce doğru mevcut kaydı QBO'da düzelt.

Çek adayları, çek tarihinden sonraki 90 günlük aralıkta aranır. Çekin düzenleme tarihi ile bankada işlenme tarihi farklı olabilir; aday ve kaynak incelemesi bu yüzden zorunludur. Tek belgede tek fiziksel çek desteklenir; ön/arka yüzü aynı PDF'de olabilir. Kaynak PDF bütünüyle eklenir. HEIC kaynak için kalıcı PNG kopyası eklenir.

## 3. Kredi kartı ekstresi — gerçek sandbox kontrolü

1. `test-data/adim110_credit_card.png` dosyasını yükle; **Verileri çıkar** düğmesine bas.
2. Dönem `2026-09-01`–`2026-09-30` ve `2026-09-15` tarihli `-550.00` Software satırını kontrol et. Gerekiyorsa **Çıkarılan alanları düzelt** bölümünden dönem ve işlem satırlarını düzeltip kaydet. Eklenen/silinen satırlar da kaydedilir.
3. Tutar düzeni: harcama/ücret/faiz **negatif**; iade/kart ödemesi **pozitif**. Dönem dışı tarih, sıfır veya geçersiz tutar eşleşmeyi engeller.
4. QBO sandbox'ta CreditCard hesabında aynı tarih/tutarlı mevcut bir Purchase kaydı bulunmalı. **QBO hesaplarını yükle** → **QBO kredi kartı hesabı** seç. Bu listede Bank hesabı bulunmamalı.
5. **QBO ile eşleştir** → her eşleşmeyi onayla/seç/reddet → **Satıcıları kontrol et** → **Kategori önerilerini oluştur** → her kategori kararını kaydet.
6. **Ekstre planını önizle** → **Onaylanan kategorileri ve çek eklerini aktar**. Bu ekstrede çek bulunmadığı için çek eki yüklenmez; onaylanan mevcut Purchase kategorileri güncellenir.
7. Aynı aktarımı tekrar dene: yeni finansal işlem ve tekrarlanan kategori yazımı oluşmamalı. Yeniden başlatmada aynı sonuç korunmalı.
8. JSON dışa aktarımını `credit_card_statement` filtresiyle al; işlem satırlarının çıktıda bulunduğunu doğrula.

Çok sayfalı kart ekstresinde bütün sayfalar okunur. Birbirine eşit gerçek işlem satırları otomatik silinmez. Kart borcu ödemesi ve Transfer, yeni gider oluşturacak şekilde işlenmez; kategori yazımı yalnız mevcut Purchase kayıtlarına uygulanır.

## 4. Toplu aktarım ve hata kontrolleri

- İki türü de toplu yükleyebilirsin. Her belgeyi inceleyip önizledikten sonra **Ekstre planını toplu aktarım için kaydet**. Toplu listede açıkça seçilen planları gönder.
- Alan düzeltmesi eski eşleşme/kategori kararlarını temizler ve kaydedilmiş toplu planı geçersiz kılar. Yeniden eşleştirip incele ve yeni plan kaydet.
- Eşleşme/kategori kararları beklerken veya QBO şirketi/önizleme revizyonu değiştiğinde yazım engellenmeli.
- QBO yazımının sonucu belirsizse yeniden gönderme yerine **QBO işlem kontrolü ve hata toparlanması** bölümünü kullan. Mevcut kategori/ek kanıtı doğrulanmadan ikinci yazıma geçilmez.
- Başka şirketin kullanıcısıyla belge, kaynak ve inceleme URL'lerini dene: erişim 404 olmalı.

## Doğrulama kaydı

- Yeni türler ve JSON export dahil hedefli testler: **85 geçti**; bunun **30'u** yeni test dosyasında.
- Tam test paketi: **553 geçti**.
- Chromium: **7 kontrol grubu geçti** — çek ve kart akışları, yeni Bill/Expense oluşmaması/tek çek eki, yeniden başlatma, gerçek oturumlarla şirket ayrımı, mobil sayfa genişliği, JavaScript hata kontrolü.
- SDK nesneleri ve API akışları test edildi; dış AI/QBO servisleri otomatik kontrollerde taklit edildi. Gerçek sandbox kabulü yukarıdaki adımlarla yapılmalı.

110 tamamlanma kriteri: gerçek extraction ve inceleme çalışmalı; doğru hesap türü seçilmeli; çek mevcut doğru işleme tek kaynak ekiyle bağlanmalı; kredi kartı ekstresi onaylanan mevcut Purchase kategorilerini güncellemeli; tekrar yazım oluşmamalı; kayıtlar yeniden başlatmada korunmalı.

Sonraki planlanan adım **111: Local/S3/R2 storage sınıflarını aktif belge akışına bağlamak**.
