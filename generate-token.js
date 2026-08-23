const { generate } = require('youtube-po-token-generator');

async function main() {
  try {
    const result = await generate();
    // دەرئەنجامەکە وەک JSON چاپ دەکات تا پایتۆن بیخوێنێتەوە
    console.log(JSON.stringify(result));
  } catch (error) {
    console.error(JSON.stringify({ error: error.message }));
    process.exit(1);
  }
}

main();