> 🚧 **Durum: aktif geliştirme.** Yazar için çalışıyor; arayüzler değişebilir.

# lesson-trigger-eval

Yalnız Python standart kütüphanesiyle yazılmış küçük bir araç: bir "ders" ya da "hatırlatma" tetikleyicisinin **ne kadar iyi çalıştığını ölçer**. Kısa derslerden oluşan bir katalog ve etiketli olaylar verildiğinde, tetikleyiciniz doğru dersi doğru anda gösteriyor mu, gereksiz yere susmuyor ya da konuşmuyor mu?

Kodlama ajanı kancalarına hatırlatma bağlayanlar için ("force-push öncesi önce sor" gibi) ve izlenim yerine sayı isteyenler için yazıldı. Tetikleyiciniz, stdin'den tek bir JSON olay okuyup göstereceği dersleri yazdıran herhangi bir programdır. Araç onu her senaryo için taze bir süreçte çalıştırır; duyarlılık (recall), kesinlik (precision), yanlış alarm oranı ve gecikmeyi raporlar.

## Hızlı başlangıç

Python 3.12 veya üstü gerekir. Depo kökünden çalıştırın:

```
python lesson_eval.py --report report.json --max-p95-ms 5000 --candidate python examples/toy_candidate.py
```

Bu komut, dahili oyuncak adayı (düz alt dizgi eşleyici) dahili örnek veriyle (5 ders, 15 senaryo) değerlendirir ve `report.json` yazar. Kalite kapısı geçerse çıkış kodu 0, geçmezse 1'dir. Testler: `python -m unittest` (ya da `python lesson_eval.py --self-test`).

## Nasıl çalışır

1. **Dersler** (`examples/lessons.json`): her birinde `id` (`L001`), `source` (`yol:satır`), ders metni, tetik açıklaması (olaylar + desen) ve aynı dosyadaki birden çok dersi ayırt etmek için `match_phrases` bulunur.
2. **Senaryolar** (`examples/scenarios.json`): her birinde `client`, `event` (`user_prompt`, `pre_tool_bash`, `pre_tool_edit`, `pre_tool_agent`, `session_start`, `task_card`), `input`, `kind` (`positive`, `negative`, `near_negative`) ve beklenen (ve yalnız kabul edilebilir) dersler vardır. Yakın negatif, aynı konudadır ama ders uygulanmaz.
3. **Aday protokolü**: stdin'e `{"client", "event", "input", "cwd"}` gelir (doğru cevap asla gönderilmez). stdout, `{"results":[{"id":"L001","source":"examples/lessons.md:3"}]}` gibi JSON ya da ders kimliği veya `yol:satır` kaynağı geçen düz metin olabilir. Sıfırdan farklı çıkış kodu ya da zaman aşımı hata sayılır; boş (başarılı) cevap sayılmaz.
4. **Puanlama**: recall@k (varsayılan k=3), adayın yazdığı her şey üzerinden precision (bilinmeyen ya da ayrıştırılamayan öğe aleyhine sayılır), negatif ve yakın negatif senaryolarda yanlış alarm oranı, soğuk başlangıç gecikmesinin medyanı ve p95'i, istemci x olay matrisi ve sayısal kimliğe göre %70/%30 geliştirme/ayrılmış küme bölünmesi. **Kapı**, bunları sizin belirlediğiniz eşiklerle karşılaştırır.

Yararlı seçenekler: `--adapter claude-hook` (kanonik yük yerine Claude Code tarzı kanca girdisi gönderir; desteklenmeyen olaylar atlandı diye raporlanır), `--source-roots roots.json` (`scheme://alias/yol` kaynaklarını klasörlere eşler), `--wrapper-prefix METİN` (kancanın başlık satırını yok sayar), `--min-recall`, `--min-precision`, `--max-false-alarm`, `--max-p95-ms`, `--min-positive` ve diğer `--min-*` veri boyutu denetimleri, `--split development|holdout`, `--validate-only`. Ayrıntı: `python lesson_eval.py --help`.

## Gereksinimler

Python 3.12+, üçüncü taraf paket yok. Yazarın Windows makinesinde denendi. Ubuntu ve Windows için bir CI iş akışı eklidir ama henüz çalışmadı.

## Sınırlar ve dürüst notlar

- **Sandbox değildir.** Senaryo girdileri veri olarak geçer, asla çalıştırılmaz; ama aday program sizin yetkilerinizle çalışır, dosya okuyabilir, ağ kullanabilir ve çalıştırıcının ulaşamayacağı ayrık alt süreçler bırakabilir. Adayı kendiniz inceleyin.
- Gecikme senaryo başına **soğuk başlangıçtır** (süreç açma + çalışma); ağırlıklı olarak yorumlayıcının açılış süresidir, sıcak ve uzun yaşayan bir servisi ölçmez.
- Her dersin `yol:satır` kaynağı olmalıdır; çıktı ayrıştırıcı `L001` biçimli kimlikleri ve `.md`/`.txt` kaynaklarını arar (yolda boşluk olmaz). Başka biçimde yazan adaylar "eşleşmedi" sayılır.
- Kaynakla eşleme dosya ve kesişen satır aralığında kesindir; aynı dosyadaki iki ders yalnız `match_phrases` ifadelerinin aynı çıktı satırında geçmesiyle ayrılır.
- Dahili veri ve oyuncak aday bilerek çok küçüktür; gerçek bir tetikleyici hakkında hiçbir şey kanıtlamaz. `claude-hook` yükleri Claude Code kanca girdilerinin asgari yaklaşıklarıdır, her sürümle denenmemiştir.
- Araç, tetikleyiciyi bir insanın yazdığı etiketlere göre ölçer; sayıların anlamı etiket kalitesiyle sınırlıdır.

English version: [README.md](README.md)

## Lisans

Apache-2.0, bkz. [LICENSE](LICENSE) ve [NOTICE](NOTICE).
