package io.github.vladimirsnow.ytcookies;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Intent;
import android.content.res.ColorStateList;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.PersistableBundle;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;
import org.json.JSONArray;
import org.json.JSONObject;
import org.mozilla.geckoview.AllowOrDeny;
import org.mozilla.geckoview.GeckoResult;
import org.mozilla.geckoview.GeckoRuntime;
import org.mozilla.geckoview.GeckoRuntimeSettings;
import org.mozilla.geckoview.GeckoSession;
import org.mozilla.geckoview.GeckoView;
import org.mozilla.geckoview.WebExtension;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.text.DateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;
import java.util.UUID;
import java.lang.ref.WeakReference;

public final class MainActivity extends Activity {
    private static final String ROBOTS = "https://www.youtube.com/robots.txt";
    private static final String LOGIN = "https://accounts.google.com/ServiceLogin?service=youtube&continue=https%3A%2F%2Fwww.youtube.com%2F";
    private static final int IMPORT = 10, SAVE_ENV = 11;
    private static GeckoRuntime runtime;
    private static WebExtension extension;
    private static WebExtension.Port port;
    private static WeakReference<MainActivity> active = new WeakReference<>(null);
    private static final WebExtension.PortDelegate PORT_DELEGATE = new WebExtension.PortDelegate() {
        @Override public void onPortMessage(Object message, WebExtension.Port source) {
            MainActivity activity = active.get();
            if (activity != null && source == port && message instanceof JSONObject) activity.receiveExport((JSONObject) message);
        }
        @Override public void onDisconnect(WebExtension.Port disconnected) {
            if (port == disconnected) port = null;
        }
    };
    private static final WebExtension.MessageDelegate MESSAGE_DELEGATE = new WebExtension.MessageDelegate() {
        @Override public void onConnect(WebExtension.Port connected) {
            port = connected; connected.setDelegate(PORT_DELEGATE);
            MainActivity activity = active.get();
            if (activity != null && activity.exportButton != null) activity.exportButton.setEnabled(true);
        }
    };
    private GeckoSession session;
    private GeckoView browser;
    private TextView origin, browserStatus;
    private Button exportButton;
    private CookieCodec.Export exported;
    private String currentUrl = "", pendingExport;
    private boolean exportRequested;
    private int browserGeneration;
    private final Handler handler = new Handler(Looper.getMainLooper());

    @Override public void onCreate(Bundle savedState) {
        super.onCreate(savedState);
        active = new WeakReference<>(this);
        if ((getApplicationInfo().flags & android.content.pm.ApplicationInfo.FLAG_DEBUGGABLE) == 0)
            getWindow().setFlags(WindowManager.LayoutParams.FLAG_SECURE, WindowManager.LayoutParams.FLAG_SECURE);
        if (Build.VERSION.SDK_INT >= 30) getWindow().setDecorFitsSystemWindows(false);
        try { String saved = SecureStore.load(this); if (saved != null) exported = CookieCodec.parse(saved); }
        catch (Exception ignored) { /* No secret values or exception payloads go to logs. */ }
        showHome(null);
    }

    private int dp(int value) { return Math.round(value * getResources().getDisplayMetrics().density); }
    private LinearLayout column() {
        LinearLayout box = new LinearLayout(this); box.setOrientation(LinearLayout.VERTICAL);
        box.setBackgroundColor(Color.rgb(244,246,250));
        box.setPadding(dp(20), dp(16), dp(20), dp(16)); return box;
    }
    private void install(View view) {
        final int left=view.getPaddingLeft(),top=view.getPaddingTop(),right=view.getPaddingRight(),bottom=view.getPaddingBottom();
        setContentView(view);
        if (Build.VERSION.SDK_INT >= 30) view.setOnApplyWindowInsetsListener((v, insets) -> {
            var bars = insets.getInsets(android.view.WindowInsets.Type.systemBars() | android.view.WindowInsets.Type.ime());
            v.setPadding(left + bars.left, top + bars.top, right + bars.right, bottom + bars.bottom);
            return insets;
        });
        else view.setFitsSystemWindows(true);
        view.requestApplyInsets();
    }
    private TextView text(LinearLayout parent, String value, int size) {
        TextView item = new TextView(this); item.setText(value); item.setTextSize(size);
        item.setTextColor(Color.rgb(16,36,58)); item.setPadding(0,dp(6),0,dp(10));
        parent.addView(item, new LinearLayout.LayoutParams(-1,-2)); return item;
    }
    private Button button(LinearLayout parent, String title, Runnable click) {
        Button b = new Button(this); b.setText(title); b.setTextSize(15);b.setAllCaps(false);
        b.setMinHeight(dp(52)); b.setBackgroundTintList(ColorStateList.valueOf(Color.rgb(27,98,209)));
        b.setTextColor(Color.WHITE);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(-1,-2); lp.topMargin=dp(7);
        parent.addView(b,lp); b.setOnClickListener(v -> click.run()); return b;
    }
    private void showHome(String notice) {
        ScrollView scroll = new ScrollView(this);scroll.setFillViewport(true);scroll.setBackgroundColor(Color.rgb(244,246,250));
        LinearLayout content = column();scroll.addView(content);install(scroll);
        text(content,"YouTube Cookies",28);
        text(content,"Сессия YouTube → значение для Render",17);
        if (notice != null) text(content, notice, 16);
        if (exported != null) {
            long time = getSharedPreferences("export",MODE_PRIVATE).getLong("savedAt",0);
            text(content,"Готово: " + exported.count + " cookies YouTube\nСохранено " + DateFormat.getDateTimeInstance(DateFormat.SHORT,DateFormat.SHORT).format(new Date(time)),16);
            button(content,"Копировать для Render",this::copyValue);
            button(content,"Сохранить файл .env",this::saveEnv);
            button(content,"Открыть Render",() -> external("https://dashboard.render.com/"));
            text(content,"В Render → Environment замени YOUTUBE_COOKIES_BASE64 и нажми Save, rebuild, and deploy. Повторное копирование не открывает YouTube.",15);
        }
        button(content,exported == null ? "Новая сессия YouTube" : "Войти заново и получить новые cookies",() -> {
            if (exported == null) startBrowser();
            else new AlertDialog.Builder(this).setTitle("Новая сессия")
                    .setMessage("Откроется новый вход в YouTube. Предыдущий экспорт сохранится, пока новый не будет готов.")
                    .setPositiveButton("Продолжить",(d,w)->startBrowser()).setNegativeButton("Отмена",null).show();
        });
        button(content,"Импорт cookies.txt",this::importFile);
        button(content,"Как пользоваться",this::showHelp);
        if (exported != null) button(content,"Удалить локальную копию",() -> new AlertDialog.Builder(this)
                .setTitle("Удалить данные приложения?")
                .setMessage("Удалятся экспорт и cookies этого приложения. Настройки Render останутся прежними.")
                .setPositiveButton("Удалить",(d,w)-> {
                    SecureStore.clear(this); exported=null;
                    if (runtime != null) runtime.getStorageController().clearData(org.mozilla.geckoview.StorageController.ClearFlags.ALL);
                    showHome("Локальные данные удалены.");
                }).setNegativeButton("Отмена",null).show());
        text(content,"Вход происходит на сайтах Google и YouTube. Приложение не отправляет экспорт в Telegram или на сервер разработчика. Сохранённый экспорт зашифрован на этом устройстве.",14);
    }

    private void startBrowser() {
        try {
            if (runtime == null) runtime = GeckoRuntime.create(getApplicationContext(),
                    new GeckoRuntimeSettings.Builder().consoleOutput(false).remoteDebuggingEnabled(false).build());
            closeSession();
            final int generation=browserGeneration;
            LinearLayout root=column();
            text(root,"Отдельная сессия YouTube",20);
            origin=text(root,"Открываю защищённую страницу Google…",14);
            browserStatus=text(root,"Войди в аккаунт. Когда откроется YouTube, нажми «Получить cookies».",14);
            LinearLayout toolbar=new LinearLayout(this);root.addView(toolbar,new LinearLayout.LayoutParams(-1,-2));
            Button back=new Button(this);back.setText("Назад");toolbar.addView(back,new LinearLayout.LayoutParams(0,dp(48),1));
            back.setOnClickListener(v->{ if(session!=null)session.goBack(); });
            Button close=new Button(this);close.setText("Закрыть");toolbar.addView(close,new LinearLayout.LayoutParams(0,dp(48),1));
            close.setOnClickListener(v->{closeSession();showHome(null);});
            exportButton=button(root,"Получить cookies",this::requestExport);exportButton.setEnabled(false);
            browser=new GeckoView(this);root.addView(browser,new LinearLayout.LayoutParams(-1,0,1));install(root);
            runtime.getWebExtensionController().ensureBuiltIn("resource://android/assets/cookiebridge/","cookiebridge@tg-bot.local")
                    .accept(ext->runOnUiThread(()->{
                        if (generation!=browserGeneration || browser==null || isFinishing()) return;
                        extension=ext;
                        ext.setMessageDelegate(MESSAGE_DELEGATE,"cookiebridge");
                        if (port != null) { port.setDelegate(PORT_DELEGATE); exportButton.setEnabled(true); }
                        openSession(generation);
                    }),err->runOnUiThread(()->{if(generation!=browserGeneration)return;closeSession();showHome("Не удалось запустить браузер. Можно импортировать cookies.txt из Firefox.");}));
        } catch(Exception ignored) {closeSession();showHome("Не удалось запустить браузер. Воспользуйся импортом cookies.txt.");}
    }

    private void openSession(int generation) {
        if(browser==null)return;
        runtime.getStorageController().clearData(org.mozilla.geckoview.StorageController.ClearFlags.COOKIES
                | org.mozilla.geckoview.StorageController.ClearFlags.DOM_STORAGES).accept(unused->runOnUiThread(()->{
            if(generation!=browserGeneration || browser==null || isFinishing())return;
            session=new GeckoSession();
            session.setContentDelegate(new GeckoSession.ContentDelegate(){});
            session.setNavigationDelegate(new GeckoSession.NavigationDelegate(){
                @Override public void onLocationChange(GeckoSession s,String uri,List<GeckoSession.PermissionDelegate.ContentPermission> permissions,Boolean gesture) {
                    if(s!=session)return;
                    currentUrl=uri;
                    Uri parsed=Uri.parse(uri);String host=parsed.getHost();
                    if(origin!=null)origin.setText("https://"+(host==null ? "…" : host));
                }
                @Override public GeckoResult<AllowOrDeny> onLoadRequest(GeckoSession s,GeckoSession.NavigationDelegate.LoadRequest request) {
                    Uri u=Uri.parse(request.uri);String host=u.getHost();
                    boolean allowed="https".equals(u.getScheme()) && host!=null
                            && (host.equals("youtube.com") || host.endsWith(".youtube.com") || host.equals("google.com") || host.endsWith(".google.com"));
                    if(!allowed && browserStatus!=null)browserStatus.setText("Переход разрешён только на HTTPS-страницы Google и YouTube.");
                    return GeckoResult.fromValue(allowed?AllowOrDeny.ALLOW:AllowOrDeny.DENY);
                }
                @Override public GeckoResult<GeckoSession> onNewSession(GeckoSession s,String uri) {
                    if(session!=null)session.loadUri(uri);return GeckoResult.fromValue(null);
                }
            });
            session.setProgressDelegate(new GeckoSession.ProgressDelegate(){
                @Override public void onPageStop(GeckoSession s,boolean success) {
                    if(s!=session)return;
                    if(pendingExport!=null && !exportRequested && currentUrl.startsWith(ROBOTS)) {
                        if(success)sendExport();else failExport("Не удалось открыть robots.txt. Проверь сеть и попробуй ещё раз.");
                    }
                }
            });
            session.open(runtime);browser.setSession(session);session.loadUri(LOGIN);
        }),err->runOnUiThread(()->{if(generation!=browserGeneration)return;closeSession();showHome("Не удалось подготовить отдельную сессию.");}));
    }

    private void requestExport() {
        if(session==null || port==null) {toast("Браузер ещё запускается. Подожди несколько секунд.");return;}
        pendingExport=UUID.randomUUID().toString();exportRequested=false;
        exportButton.setEnabled(false);browserStatus.setText("Завершаю страницу YouTube и сохраняю cookies…");
        String request=pendingExport;
        handler.postDelayed(()->{if(request.equals(pendingExport))failExport("Экспорт не завершился. Проверь соединение и повтори.");},25000);
        session.loadUri(ROBOTS);
    }
    private void sendExport() {
        if(port==null){failExport("Нет соединения с браузером. Открой вход заново.");return;}
        try {exportRequested=true;JSONObject request=new JSONObject();request.put("action","export");request.put("requestId",pendingExport);port.postMessage(request);}
        catch(Exception ignored){failExport("Не удалось запросить cookies.");}
    }
    private void receiveExport(JSONObject message) {
        if(pendingExport==null || !pendingExport.equals(message.optString("requestId")))return;
        if(!message.optString("type").equals("cookies")){failExport("Не удалось получить cookies. Можно импортировать файл из Firefox.");return;}
        try {
            JSONArray rows=message.getJSONArray("cookies");List<CookieCodec.Cookie> cookies=new ArrayList<>();
            for(int i=0;i<rows.length();i++) {
                JSONObject c=rows.getJSONObject(i);
                Long expiry=c.has("expirationDate")&&!c.isNull("expirationDate") ? (long)Math.floor(c.getDouble("expirationDate")) : null;
                cookies.add(new CookieCodec.Cookie(c.getString("domain"),c.optString("path","/"),c.getString("name"),c.getString("value"),
                        !c.optBoolean("hostOnly",false),c.optBoolean("secure",false),c.optBoolean("httpOnly",false),expiry));
            }
            CookieCodec.Export ready=CookieCodec.build(cookies);SecureStore.save(this,ready.netscape);
            exported=ready;pendingExport=null;closeSession();showHome("Cookies готовы. Страница YouTube закрыта.");
        }catch(IllegalArgumentException e){failExport(e.getMessage());}
        catch(Exception ignored){failExport("Не удалось сохранить экспорт. Можно импортировать cookies.txt.");}
    }
    private void failExport(String message) {
        pendingExport=null;exportRequested=false;
        if(exportButton!=null)exportButton.setEnabled(port!=null);
        if(browserStatus!=null)browserStatus.setText(message);
    }
    private void closeSession() {
        browserGeneration++;
        pendingExport=null;exportRequested=false;
        if(browser!=null)browser.releaseSession();
        if(session!=null){session.close();session=null;}
        browser=null;origin=null;browserStatus=null;exportButton=null;
    }

    private void importFile() {
        Intent intent=new Intent(Intent.ACTION_OPEN_DOCUMENT);intent.addCategory(Intent.CATEGORY_OPENABLE);intent.setType("*/*");
        startActivityForResult(intent,IMPORT);
    }
    private void saveEnv() {
        if(exported==null)return;
        Intent intent=new Intent(Intent.ACTION_CREATE_DOCUMENT);intent.addCategory(Intent.CATEGORY_OPENABLE);
        intent.setType("text/plain");intent.putExtra(Intent.EXTRA_TITLE,"render-youtube.env");startActivityForResult(intent,SAVE_ENV);
    }
    @Override public void onActivityResult(int request,int result,Intent data) {
        super.onActivityResult(request,result,data);
        if(result!=RESULT_OK || data==null || data.getData()==null)return;
        Uri uri=data.getData();
        if(request==IMPORT) {
            new Thread(()->{
                try(InputStream input=getContentResolver().openInputStream(uri)) {
                    if(input==null)throw new IllegalArgumentException("Не удалось открыть файл.");
                    ByteArrayOutputStream buffer=new ByteArrayOutputStream();byte[] chunk=new byte[4096];int n;
                    while((n=input.read(chunk))!=-1){buffer.write(chunk,0,n);if(buffer.size()>CookieCodec.MAX_BYTES)throw new IllegalArgumentException("Файл больше 128 КБ.");}
                    CookieCodec.Export ready=CookieCodec.parse(buffer.toString("UTF-8"));SecureStore.save(this,ready.netscape);
                    runOnUiThread(()->{exported=ready;showHome("Импортированы только cookies YouTube. Остальные домены исключены.");});
                }catch(IllegalArgumentException e){runOnUiThread(()->showHome(e.getMessage()));}
                catch(Exception ignored){runOnUiThread(()->showHome("Не удалось прочитать или сохранить файл."));}
            }).start();
        } else if(request==SAVE_ENV && exported!=null) {
            try(OutputStream output=getContentResolver().openOutputStream(uri,"wt")) {
                if(output==null)throw new IllegalStateException();output.write(exported.env().getBytes(StandardCharsets.UTF_8));toast("Файл .env сохранён. Не отправляй его в чат.");
            }catch(Exception ignored){toast("Не удалось сохранить файл.");}
        }
    }
    private void copyValue() {
        if(exported==null)return;
        ClipData clip=ClipData.newPlainText("YouTube cookies for Render",exported.base64);
        if(Build.VERSION.SDK_INT>=33){PersistableBundle extras=new PersistableBundle();extras.putBoolean("android.content.extra.IS_SENSITIVE",true);clip.getDescription().setExtras(extras);}
        ((ClipboardManager)getSystemService(CLIPBOARD_SERVICE)).setPrimaryClip(clip);
        toast("Скопировано только значение YOUTUBE_COOKIES_BASE64.");
    }
    private void external(String url) {
        try{startActivity(new Intent(Intent.ACTION_VIEW,Uri.parse(url)));}catch(Exception ignored){toast("Не найден браузер.");}
    }
    private void showHelp() {
        new AlertDialog.Builder(this).setTitle("Как получить cookies")
                .setMessage("1. Нажми «Новая сессия YouTube» и войди на странице Google.\n2. Когда откроется YouTube, нажми «Получить cookies».\n3. Нажми «Копировать для Render». В Environment замени YOUTUBE_COOKIES_BASE64 и нажми Save, rebuild, and deploy.\n\nЕсли Google не разрешает вход: используй Firefox для Android и расширение cookies.txt. Экспортируй только YouTube в отдельном сеансе, закрой его и выбери «Импорт cookies.txt» здесь.\n\nЭкспорт даёт доступ к сессии. Не публикуй его и не отправляй в Telegram. После экспорта не открывай этот сеанс для просмотра видео. Повторный вход создавай только когда понадобятся новые cookies.\n\nGoogle может отозвать сессию; бессрочная работа не гарантируется.")
                .setPositiveButton("Понятно",null)
                .setNeutralButton("Расширение Firefox",(d,w)->external("https://addons.mozilla.org/firefox/addon/cookies-txt/")).show();
    }
    private void toast(String text){Toast.makeText(this,text,Toast.LENGTH_LONG).show();}
    @Override public void onBackPressed() {
        if(session!=null){closeSession();showHome(null);}else super.onBackPressed();
    }
    @Override public void onDestroy(){closeSession();if(active.get()==this)active.clear();super.onDestroy();}
}
