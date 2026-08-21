// خوێندنەوەی زمانی پاشەکەوتکراو یان بەکارهێنانی ئینگلیزی وەک سەرەتایی
let currentLang = localStorage.getItem('app_lang') || 'en';

function setLanguage(lang) {
    localStorage.setItem('app_lang', lang);
    fetch(`/static/locales/${lang}.json`)
        .then(response => response.json())
        .then(translations => {
            // گۆڕینی دەقەکانی ناو HTML بەپێی وەرگێڕانەکە
            document.querySelectorAll('[data-i18n]').forEach(element => {
                const key = element.getAttribute('data-i18n');
                if (translations[key]) {
                    if (element.tagName === 'INPUT' && element.placeholder) {
                        element.placeholder = translations[key];
                    } else {
                        element.innerText = translations[key];
                    }
                }
            });
            // گۆڕینی ئاڕاستەی نووسین (RTL بۆ کوردی/عەرەبی)
            document.dir = (lang === 'ku' || lang === 'ar') ? 'rtl' : 'ltr';
        });
}

// لە کاتی بەگەڕکەوتنی لاپەڕەکە
document.addEventListener('DOMContentLoaded', () => {
    setLanguage(currentLang);
});