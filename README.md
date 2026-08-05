[![HACS Default](https://img.shields.io/badge/HACS-Default-blue.svg)](https://github.com/hacs/default)
[![GitHub release](https://img.shields.io/github/release/myTselection/delhaize_ha.svg)](https://github.com/myTselection/delhaize_ha/releases)
![GitHub repo size](https://img.shields.io/github/repo-size/myTselection/delhaize_ha.svg)

# Delhaize Home Assistant integration

Home Assistant custom integration for [Delhaize](https://www.delhaize.be/) SuperPlus market loyalty points: savings, loyalty points, and personal e-Deals.

The integration provides an overview of your [Delhaize personal profile](https://www.delhaize.be/login): number of points, amount saved and information on personal promo's. The personal promo's can be auto activated as soon as they become available.

This integration talks to the same Delhaize GraphQL endpoint used by the website (`https://www.delhaize.be/api/v1/`). It is not affiliated with Delhaize. Most of it has been created by ChatGPT Codex.

| :warning: Please do not report integration issues to Delhaize. They will not be able to support this custom component. |
| -------------------------------------------------------------------------------------------------------------------- |


<p align="center"><img src="https://raw.githubusercontent.com/myTselection/delhaize_ha/master/logo.png"/></p>



## Installation

- [HACS](https://hacs.xyz/): search for delhaize_ha in the default HACS repo list or use below button to navigate directly to it on your local system and install via HACS. 
   -    [![Open your Home Assistant instance and open the repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg?style=flat-square)](https://my.home-assistant.io/redirect/hacs_repository/?owner=myTselection&repository=delhaize_ha&category=integration)
- Restart Home Assistant.
- Add the `Delhaize` integration from Settings > Devices and services.
- <img src="https://raw.githubusercontent.com/myTselection/delhaize_ha/master/setup.png"/>
- The cookie header input will be requested.
   - Open a separate browser and login in on the site https://www.delhaize.be/my-account/dashboard,
   - Login in the browser, if needed with email temporary code confirmation
   - Open the Developer Tools of the browser (F12) and select 'Network' tab.
   - Navigate to the user profile on the Delhaize site
   - In the DevTool Network Tab, select a connection loaded from "https://www.delhaize.be/api/v1/..." and search for the "Request Headers" > "Cookie".
   - Copy and paste the full "Cookie" value into the Home Assistant "cookie" field. This will allow Home Assistant to re-use the authenticated browser session.
   - <img src="https://raw.githubusercontent.com/myTselection/delhaize_ha/master/cookiefrombrowser.png"/>
- When the option "Automatically activate personal offers" is enabled and personal offers are detected which haven't been activated yet, these will automatically be activated.
   - This will make sure all personal offers are immediately available to you    
- The configuration options can still be changed after setup has been completed
- After setup, use the integration's Configure/Options screen to update language, automatic offer activation, credentials, or the Cookie header.


<details><summary><b>Authentication background info</b></summary>

  Delhaize currently exposes a website login mutation, but the public login page usually requires captcha before MFA can start. Home Assistant cannot solve Delhaize captcha challenges, so Cookie authentication is the default setup path.

  - Logged-in browser Cookie header: recommended and selected by default. Log in to `https://www.delhaize.be/` in a browser, copy the request `Cookie` header from an authenticated request to `/api/v1/`, and paste it into the setup flow.
  - Username and password: optional best-effort path. Home Assistant submits the same website login mutation as Delhaize.be.
  - Email temporary code: only works when Delhaize accepts the password step and then requires MFA. If Delhaize rejects the password step with `captcha_invalid_error`, no email code is sent and Cookie authentication is required.

  The integration stores refreshed Delhaize cookies in the Home Assistant config entry so the session can survive restarts as long as Delhaize keeps the session valid.

</details>

## Entities

The integration creates sensors for:

- Loyalty points
- Savings
- Personal offers available
- Personal offers total
- Personal offers activated
- Personal offers benefit
- Loyalty profile
- Loyalty card number
- Account

<p align="center"><img src="https://raw.githubusercontent.com/myTselection/delhaize_ha/master/sensors.png"/></p>

## Dashboard example

The offer sensors expose detailed attributes that can be used in a Markdown card. This example searches for Delhaize offer sensors automatically, so it can work with one or more configured accounts without hardcoding entity IDs.

```yaml
type: markdown
title: Delhaize promotions
content: |
  {% set ns = namespace(offer_sensors=[]) %}
  {% for sensor in states.sensor %}
    {% if sensor.entity_id.startswith('sensor.delhaize_') and sensor.entity_id.endswith('_personal_offers_available') %}
      {% set ns.offer_sensors = ns.offer_sensors + [sensor.entity_id] %}
    {% endif %}
  {% endfor %}

  {% if not ns.offer_sensors %}
  No Delhaize offer sensors found.
  {% endif %}

  {% for available_entity in ns.offer_sensors %}
  {% set activated_entity = available_entity
    | replace('_personal_offers_available', '_personal_offers_activated') %}
  {% set burnable_entity = available_entity
    | replace('_personal_offers_available', '_burnable_offer_discounts') %}
  {% set account = (state_attr(available_entity, 'friendly_name') or available_entity)
    | replace(' Personal offers available', '') %}
  {% set personal = state_attr(activated_entity, 'description_list') or [] %}
  {% set flash = state_attr(available_entity, 'flash_offer_list') or [] %}
  {% set coupon_personal = state_attr(available_entity, 'coupon_book_personal_offer_list') or [] %}
  {% set burnable = state_attr(burnable_entity, 'product_discount_list') or [] %}

  ## {{ account }}

  **Personal e-Deals**  
  Activated: {{ states(activated_entity) }}

  {% if personal %}
  | Promotion | Details | Points | Until |
  |---|---|---:|---:|
  {% for offer in personal %}| {{ offer.get('description', '-') | replace('|',' ') }} | {{ offer.get('promotion', '-') }} | {{ offer.get('points', '-') }} | {{ offer.get('available_until', '-') }} |
  {% endfor %}
  {% else %}
  No personal e-Deals found.
  {% endif %}

  {% if coupon_personal %}
  **Coupon book personal offers**

  | Promotion | Details | Status | Until |
  |---|---|---:|---:|---:|
  {% for offer in coupon_personal %}| {{ offer.get('description', '-') | replace('|',' ') }} | {{ offer.get('promotion', '-') }} | {{ 'Active' if offer.get('active') else 'Available' }} | {{ offer.get('available_until', '-') }} |
  {% endfor %}
  {% endif %}

  **Flash e-Deals**  
  Available: {{ state_attr(available_entity, 'flash_available') or 0 }} /
  {{ state_attr(available_entity, 'flash_total') or 0 }}

  {% if flash %}
  | Promotion | Details | Type | Status | Points | Until |
  |---|---|---:|---:|---:|---:|
  {% for offer in flash %}| {{ offer.get('description', '-')| replace('|',' ')  }} | {{ offer.get('promotion', '-') }} | {{ offer.get('promotion_type', '-') }} | {{ 'Active' if offer.get('active') else 'Available' }} | {{ offer.get('points', '-') }} | {{ offer.get('available_until', '-') }} |
  {% endfor %}
  {% else %}
  No flash e-Deals found.
  {% endif %}

  **Burnable product discounts**
  Products: {{ states(burnable_entity) }}

  {% if burnable %}
  | Product | Offer | Point price | Original | Discount | Remaining |
  |---|---|---:|---:|---:|---:|
  {% for product in burnable %}| {{ product.get('product', '-') | replace('|',' ') }} | {{ product.get('offer', '-') | replace('|',' ') }} | €{{ product.get('point_price_value', '-') }} / {{ product.get('discount_points', '-') }} pts | {{ product.get('original_price', '-') }} | {{ product.get('discount_percentage_formatted', '-') }} | {{ product.get('days_remaining', '-') }} days |
  {% endfor %}
  {% else %}
  No burnable product discounts found.
  {% endif %}

  {% endfor %}
```

The `promotion`, `promotion_type`, dates, prices, and point values come from Delhaize. For burnable offers, the integration exposes `discount_points`, `point_price_value`, and `discount_percentage`, where `point_price_value` assumes 1 point = €0.01 and `discount_percentage` is calculated as point price divided by the original product price. Original product price uses `price.wasPrice` when available and otherwise falls back to the normal product price.

## Services

`delhaize_ha.activate_personal_offers`

Activates available personal offers for all configured accounts, or for a specific config entry when `entry_id` is provided.

## Debug logging

Add this to `configuration.yaml` when troubleshooting:

```yaml
logger:
  default: info
  logs:
    custom_components.delhaize_ha: debug
```

## Status

Proof of concept. Delhaize can change its website GraphQL schema, captcha requirements, or cookie behavior at any time.
