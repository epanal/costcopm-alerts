import requests

url = "https://search.costco.com/api/apps/www_costco_com/query/www_costco_com_navigation?expoption=lucidworks&q=*%3A*&locale=en-US&start=0&expand=false&userLocation=CA&loc=653-bd%2C848-bd%2C423-wh%2C1251-3pl%2C1321-wm%2C1461-3pl%2C283-wm%2C561-wm%2C725-wm%2C731-wm%2C758-wm%2C759-wm%2C847_0-cor%2C847_0-cwt%2C847_0-edi%2C847_0-ehs%2C847_0-membership%2C847_0-mpt%2C847_0-spc%2C847_0-wm%2C847_1-cwt%2C847_1-edi%2C847_aa_00-spc%2C847_aa_u610-edi%2C847_d-fis%2C847_lg_n1f-edi%2C847_lux_us51-edi%2C847_NA-cor%2C847_NA-pharmacy%2C847_NA-wm%2C847_ss_u357-edi%2C847_wp_r460-edi%2C951-wm%2C952-wm%2C9847-wcs&whloc=423-wh&rows=24&url=%2Fprecious-metals.html&fq=%7B!tag%3Ditem_program_eligibility%7Ditem_program_eligibility%3A(%22ShipIt%22)&chdcategory=true&chdheader=true"

headers = {
    "User-Agent": "Mozilla/5.0 (compatible; DataCheck/1.0; +https://example.com)"
}

response = requests.get(url, headers=headers)
data = response.json()

# Print the "numFound" field inside "response"
num_found = data.get("response", {}).get("numFound")
print(f"numFound: {num_found}")
