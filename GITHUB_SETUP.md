# বগুড়া কথন - GitHub Actions অটোমেশন

## সেটআপ (একবারই)

### ১. GitHub-এ নতুন Repository বানাও
- github.com এ যাও
- New repository → নাম দাও `bogra-kothon-auto`
- Public বা Private যেকোনো

### ২. এই ফোল্ডারের ফাইলগুলো আপলোড করো
- main.py
- logo.png
- .github/workflows/auto_post.yml ফোল্ডারসহ

### ৩. Facebook Secrets যোগ করো
Repository → Settings → Secrets and variables → Actions → New repository secret

দুইটা secret যোগ করো:
- `FACEBOOK_PAGE_ID` = তোমার পেজ আইডি
- `FACEBOOK_ACCESS_TOKEN` = তোমার Page Access Token

### ৪. চালু করো
Actions ট্যাবে গিয়ে "Bogra Kothon Auto Post" workflow ম্যানুয়ালি Run করতে পারবে।
শিডিউল অনুযায়ী প্রতি ২ ঘণ্টায় অটো চলবে।

---
**নোট:** AI রিরাইট এখনো সহজ ভার্সনে আছে। পরে আপগ্রেড করা যাবে।
